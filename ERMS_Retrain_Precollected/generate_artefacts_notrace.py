"""
This script attempts to generate the artefacts
necessary for ERMS without doing a full-scale 1-week trace.

The idea is to take pre-collected Jaeger traces, and use them, and only them to generate the
artefacts. CPU and Memory usage will be set to zero, therefore making calculations only
based on the traces.
"""

from datetime import datetime
import multiprocessing
import os
from time import sleep
import traceback
from typing import Dict, List, Set
import json
import re
import pandas as pd
import requests
import argparse

pd.options.mode.chained_assignment = None


def read_trace_data(
    jaeger_trace_file, operation=None, no_nginx=False, no_frontend=False
):
    # subsitute the trace request from offlineprofilingdatacollector
    # with just reading a precollected jaeger trace file
    res = json.loads(jaeger_trace_file)["traces"]
    if len(res) == 0:
        print(f"No traces are fetched!", "error")
        return False, None, None
    else:
        print(f"Number of traces: {len(res)}")
    # Record process id and microservice name mapping of all traces
    # Original headers: traceID, processes.p1.serviceName, processes.p2.serviceName, ...
    # Processed headers: traceId, p1, p2, ...
    service_id_mapping = (
        pd.json_normalize(res)
        .filter(regex="serviceName|traceID|tags")
        .rename(
            columns=lambda x: re.sub(
                r"processes\.(.*)\.serviceName|processes\.(.*)\.tags",
                lambda match_obj: (
                    match_obj.group(1)
                    if match_obj.group(1)
                    else f"{match_obj.group(2)}Pod"
                ),
                x,
            )
        )
        .rename(columns={"traceID": "traceId"})
    )
    service_id_mapping = (
        service_id_mapping.filter(regex=".*Pod")
        .applymap(
            lambda x: (
                [v["value"] for v in x if v["key"] == "hostname"][0]
                if isinstance(x, list)
                else ""
            )
        )
        .combine_first(service_id_mapping)
    )

    spans_data = pd.json_normalize(res, record_path="spans")[
        [
            "traceID",
            "spanID",
            "operationName",
            "duration",
            "processID",
            "references",
            "startTime",
        ]
    ]
    spans_with_parent = spans_data[~(spans_data["references"].astype(str) == "[]")]
    root_spans = spans_data[(spans_data["references"].astype(str) == "[]")]
    root_spans = root_spans.rename(
        columns={
            "traceID": "traceId",
            "startTime": "traceTime",
            "duration": "traceLatency",
        }
    )[["traceId", "traceTime", "traceLatency"]]
    spans_with_parent.loc[:, "parentId"] = spans_with_parent["references"].map(
        lambda x: x[0]["spanID"]
    )
    temp_parent_spans = spans_data[
        ["traceID", "spanID", "operationName", "duration", "processID"]
    ].rename(
        columns={
            "spanID": "parentId",
            "processID": "parentProcessId",
            "operationName": "parentOperation",
            "duration": "parentDuration",
            "traceID": "traceId",
        }
    )
    temp_children_spans = spans_with_parent[
        [
            "operationName",
            "duration",
            "parentId",
            "traceID",
            "spanID",
            "processID",
            "startTime",
        ]
    ].rename(
        columns={
            "spanID": "childId",
            "processID": "childProcessId",
            "operationName": "childOperation",
            "duration": "childDuration",
            "traceID": "traceId",
        }
    )
    # A merged data frame that build relationship of different spans
    merged_df = pd.merge(
        temp_parent_spans, temp_children_spans, on=["parentId", "traceId"]
    )

    merged_df = merged_df[
        [
            "traceId",
            "childOperation",
            "childDuration",
            "parentOperation",
            "parentDuration",
            "parentId",
            "childId",
            "parentProcessId",
            "childProcessId",
            "startTime",
        ]
    ]

    # Map each span's processId to its microservice name
    merged_df = merged_df.merge(service_id_mapping, on="traceId")
    merged_df = merged_df.merge(root_spans, on="traceId")
    merged_df = merged_df.assign(
        childMS=merged_df.apply(lambda x: x[x["childProcessId"]], axis=1),
        childPod=merged_df.apply(lambda x: x[f"{str(x['childProcessId'])}Pod"], axis=1),
        parentMS=merged_df.apply(lambda x: x[x["parentProcessId"]], axis=1),
        parentPod=merged_df.apply(
            lambda x: x[f"{str(x['parentProcessId'])}Pod"], axis=1
        ),
        endTime=merged_df["startTime"] + merged_df["childDuration"],
    )
    merged_df = merged_df[
        [
            "traceId",
            "traceTime",
            "startTime",
            "endTime",
            "parentId",
            "childId",
            "childOperation",
            "parentOperation",
            "childMS",
            "childPod",
            "parentMS",
            "parentPod",
            "parentDuration",
            "childDuration",
        ]
    ]

    # no_nginx and no_frontend are always false
    # if no_nginx:
    #     return (
    #         True,
    #         merged_df,
    #         t_processor.no_entrance_trace_duration(merged_df, "nginx"),
    #     )
    # elif no_frontend:
    #     return (
    #         True,
    #         merged_df,
    #         t_processor.no_entrance_trace_duration(merged_df, "frontend"),
    #     )
    return True, merged_df, root_spans


def tproc_construct_relationship(data: pd.DataFrame, max_edges):
    result = pd.DataFrame()

    def graph_for_trace(x: pd.DataFrame):
        nonlocal max_edges
        if len(x) <= max_edges:
            return
        max_edges = len(x)
        root = x.loc[~x["parentId"].isin(x["childId"].unique().tolist())]

        nonlocal result
        result = pd.DataFrame()

        def dfs(parent, parent_tag):
            nonlocal result
            children = x.loc[x["parentId"] == parent["childId"]]
            for index, (_, child) in enumerate(children.iterrows()):
                child_tag = f"{parent_tag}.{index+1}"
                result = pd.concat(
                    [result, pd.DataFrame(child).transpose().assign(tag=child_tag)]
                )
                dfs(child, child_tag)

        for index, (_, first_lvl) in enumerate(root.iterrows()):
            first_lvl["tag"] = index + 1
            result = pd.concat([result, pd.DataFrame(first_lvl).transpose()])
            dfs(first_lvl, index + 1)

    data.groupby(["traceId", "traceTime"]).apply(graph_for_trace)
    if len(result) != 0:
        return (
            result[
                [
                    "parentMS",
                    "childMS",
                    "parentOperation",
                    "childOperation",
                    "tag",
                    "service",
                ]
            ],
            max_edges,
        )
    else:
        return False


def construct_relationship(
    _span_data: pd.DataFrame, _max_edges: Dict, _relation_df: Dict, _service
):
    if not _service in max_edges:
        _max_edges[_service] = 0
    relation_result = tproc_construct_relationship(
        _span_data.assign(service=_service),
        _max_edges[_service],
    )
    if relation_result:
        _relation_df[_service], max_edges[_service] = relation_result
    pd.concat([x[1] for x in _relation_df.items()]).to_csv(
        f"./spanRelationships.csv", index=False
    )


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Process Jaeger trace data.")
    parser.add_argument(
        "--inputJaegerTrace",
        type=str,
        required=True,
        help="Path to the input Jaeger trace JSON file",
    )
    args = parser.parse_args()

    jaeger_trace_data = None
    success, span_data, _ = (None, None, None)
    try:
        with open(args.inputJaegerTrace, "r") as trace_file:
            jaeger_trace_data = trace_file.read()
        success, span_data, _ = read_trace_data(jaeger_trace_data)
        if success:
            print("Trace data processed successfully.")
            print(span_data)
        else:
            print("Failed to process trace data.")
    except Exception as e:
        print(f"An error occurred: {e}")
        traceback.print_exc()

    max_edges = {}
    relation_df = {}
    service = ["ComposePost", "UserTimeline", "HomeTimeline"]
    for svc in service:
        construct_relationship(span_data, max_edges, relation_df, svc)
