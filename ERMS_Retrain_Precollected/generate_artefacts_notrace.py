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


def read_trace_data(jaeger_trace_file):
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

    glist = ["traceId", "traceTime"]
    data.groupby(glist).apply(graph_for_trace)
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

    # read the generated CSV back into a dataframe, add a step column, set every row to 1 in that step column, write it back
    span_relationships = pd.read_csv(f"./spanRelationships.csv")
    span_relationships["step"] = 1
    span_relationships.to_csv(f"./spanRelationships.csv", index=False)


def _cal_exact_max(data: pd.DataFrame):
    data = data.groupby("traceId").apply(
        lambda x: x.groupby("parentId").apply(
            lambda y: y.assign(
                exactParentDuration=y["parentDuration"] - y["childDuration"].max()
            )
        )
    )
    return (
        data.drop(columns=["traceId", "parentId"]).reset_index().drop(columns="level_2")
    )


def _cal_exact_merge(data: pd.DataFrame):
    def groupByParentLevel(potentialConflicGrp: pd.DataFrame):
        conflictionGrp: List[List[int]] = []
        potentialConflicGrp.apply(
            findAllConflict,
            axis=1,
            conflictionGrp=conflictionGrp,
            potentialConflicGrp=potentialConflicGrp,
        )

        childDuration = 0
        for grp in conflictionGrp:
            grp = [i for i in grp]
            conflictChildren = potentialConflicGrp.loc[grp]
            childDuration += (
                conflictChildren["endTime"].max() - conflictChildren["startTime"].min()
            )

        potentialConflicGrp = potentialConflicGrp.assign(
            exactParentDuration=potentialConflicGrp["parentDuration"] - childDuration
        )

        return potentialConflicGrp

    def findAllConflict(
        span, conflictionGrp: List[Set[int]], potentialConflicGrp: pd.DataFrame
    ):
        myStart = span["startTime"]
        myEnd = span["endTime"]
        """
        Three different types of confliction
                -------------------
                |     ThisSpan    |
                -------------------
        ---------------------
        |     OtherSpan     |
        ---------------------
        """
        conditionOne = (potentialConflicGrp["startTime"] < myStart) & (
            myStart < potentialConflicGrp["endTime"]
        )
        """
        ------------------
        |    ThisSpan    |
        ------------------
                ---------------------
                |     OtherSpan     |
                ---------------------
        """
        conditionTwo = (potentialConflicGrp["startTime"] < myEnd) & (
            myEnd < potentialConflicGrp["endTime"]
        )
        """
        ------------------------------
        |          ThisSpan          |
        ------------------------------
            ---------------------
            |     OtherSpan     |
            ---------------------
        """
        conditionThree = (myStart < potentialConflicGrp["startTime"]) & (
            myEnd > potentialConflicGrp["endTime"]
        )
        confliction = potentialConflicGrp.loc[
            conditionOne | conditionTwo | conditionThree
        ].index.to_list()
        confliction.append(span.name)
        correspondingGroup = set()
        for group in conflictionGrp:
            founded = False
            for index in confliction:
                if index in group:
                    correspondingGroup = group
                    founded = True
                    break
            if founded:
                break
        for index in confliction:
            correspondingGroup.add(index)
        if not correspondingGroup in conflictionGrp:
            conflictionGrp.append(correspondingGroup)

    data = data.groupby("traceId").apply(
        lambda x: x.groupby("parentId").apply(groupByParentLevel)
    )

    data = (
        data.drop(columns=["traceId", "parentId"]).reset_index().drop(columns="level_2")
    )

    data = data.astype({"exactParentDuration": float, "childDuration": float})
    data = data.loc[data["exactParentDuration"] > 0]

    return data


def exact_parent_duration(data: pd.DataFrame, method):
    if method == "merge":
        return _cal_exact_merge(data)
    elif method == "max":
        return _cal_exact_max(data)


def decouple_parent_and_child(data: pd.DataFrame, percentile=0.95):
    parent_perspective = data.groupby(["parentMS", "parentPod"])[
        "exactParentDuration"
    ].quantile(percentile)
    parent_perspective.index.names = ["microservice", "pod"]
    child_perspective = data.groupby(["childMS", "childPod"])["childDuration"].quantile(
        percentile
    )
    child_perspective.index.names = ["microservice", "pod"]
    quantiled = pd.concat([parent_perspective, child_perspective])
    quantiled = quantiled[~quantiled.index.duplicated(keep="first")]
    # Parse the serie to a data frame
    data = quantiled.to_frame(name="latency")
    data = data.reset_index()
    return data


def process_span_data(span_data: pd.DataFrame):
    db_data = pd.DataFrame()
    span_data = exact_parent_duration(span_data, "merge")
    p95_df = decouple_parent_and_child(span_data, 0.95)
    p50_df = decouple_parent_and_child(span_data, 0.5)
    return (
        p50_df.rename(columns={"latency": "median"}).merge(
            p95_df, on=["microservice", "pod"]
        ),
        db_data,
    )


def append_data(data: pd.DataFrame, data_path):
    """This method is used to write data to a file, if the file is exist,
    it will append data to the end, otherwise it will create a new file.

    Args:
        data (pd.DataFrame): Data to save
        data_path (str): Path to the file
    """
    if not os.path.exists(data_path):
        open(data_path, "w").close()
    is_empty = os.path.getsize(data_path) == 0
    data.to_csv(data_path, index=False, mode="a", header=is_empty)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Process Jaeger trace data.")
    parser.add_argument(
        "--jaegerTraceDir",
        type=str,
        required=True,
        help="Path to the directory containing Jaeger trace files",
    )
    args = parser.parse_args()

    # get all the files in the trace dir into a list
    jaeger_trace_files = []
    for root, dirs, files in os.walk(args.jaegerTraceDir):
        for file in files:
            if file.endswith(".json"):
                jaeger_trace_files.append(os.path.join(root, file))

    max_edges = {}
    relation_df = {}
    for jaeger_trace_file in jaeger_trace_files:
        trace_rps = jaeger_trace_file.split("_")[-1].split(".")[0]
        jaeger_trace_data = None
        success, span_data, _ = (None, None, None)
        try:
            with open(jaeger_trace_file, "r") as trace_file:
                jaeger_trace_data = trace_file.read()
            success, span_data, _ = read_trace_data(jaeger_trace_data)
            if success:
                print("Trace data processed successfully.")
                print(span_data)
            else:
                print("Failed to process trace data.")
        except Exception as e:
            print(f"An error occurred: {e}, skipping this trace file.")
            continue
            traceback.print_exc()

        service = ["ComposePost", "UserTimeline", "HomeTimeline"]
        for svc in service:
            construct_relationship(span_data, max_edges, relation_df, svc)
            latency_by_pod, db_data = process_span_data(span_data)

            # add a cpu usage and mem usage columns zeroed out
            latency_by_pod = latency_by_pod.assign(cpuUsage=2, memUsage=2)

            print("Latency By Pod:")
            print(latency_by_pod)
            print("DB Data:")
            print(db_data)
            append_data(db_data.assign(service=svc), "db.csv")
            deployments = (
                latency_by_pod["pod"]
                .apply(lambda x: "-".join(str(x).split("-")[:-2]))
                .unique()
                .tolist()
            )
            print("Deployments:")
            print(deployments)
            latency_by_pod = latency_by_pod.assign(
                repeat=0,
                service=svc,
                cpuInter=0,
                memInter=0,
                targetReqFreq=float(int(trace_rps)),
                reqFreq=float(int(trace_rps)),
            )
            append_data(latency_by_pod, f"latencyByPod.csv")

        print(f"Finished Trace file {jaeger_trace_file}")
        # skipping cpu/mem usage
