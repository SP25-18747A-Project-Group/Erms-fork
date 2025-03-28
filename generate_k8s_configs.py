import subprocess
import pandas as pd
import io
import json


def run_shell_commands():
    # Run the first shell command
    cmd1 = subprocess.run(
        [
            "bash",
            "./AE/scripts/latency-target-computation.sh",
            "--app",
            "social-network",
            "--config",
            "./AE/scripts/configs/social-latency-psched.yaml",
            "--profiling-data",
            "AE/",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # if stderr, throw exception
    if cmd1.stderr:
        print("Latency Target Computation Errored out")
        raise subprocess.CalledProcessError(
            cmd1.returncode, cmd1.args, output=cmd1.stdout, stderr=cmd1.stderr
        )

    # Run the second shell command
    cmd2 = subprocess.run(
        [
            "bash",
            "./AE/scripts/priority-scheduling.sh",
            "--app",
            "social-network",
            "--config",
            "./AE/scripts/configs/social-latency-psched.yaml",
            "--profiling-data",
            "AE/",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if cmd2.stderr:
        print("Priority Scheduling Computation Errored out")
        raise subprocess.CalledProcessError(
            cmd2.returncode, cmd2.args, output=cmd2.stdout, stderr=cmd2.stderr
        )

    # Run the third shell command and capture its output
    result = subprocess.run(
        [
            "bash",
            "./AE/scripts/dynamic-provisioning.sh",
            "--app",
            "social-network",
            "--config",
            "./AE/scripts/configs/social-latency-psched.yaml",
            "--profiling-data",
            "AE/",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    if result.stderr:
        print("DynProv Computation Errored out")
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )

    # Postprocess the output (for now, just store it in a string and print it)
    output = result.stdout.strip()

    output = output.split("\n")
    output = output[1:]  # Skip the header
    output = output[:-1]  # Skip the last line
    df = pd.DataFrame(columns=["microservice", "replicas"])
    for i, row in enumerate(output):
        # split on whitespace
        row = row.split()
        # join all elements with comma seperators
        row = ",".join(row)
        # drop elements 0 and 2
        row = row.split(",")
        if row[1] == 'write-home-timeline-service':
            row[1] = 'home-timeline-service'

        row = [row[1], int(row[3])]
        # append to dataframe
        df = pd.concat(
            [pd.DataFrame([[row[0], row[1]]], columns=df.columns), df],
            ignore_index=True,
        )

    # do a sum on the replicas column so that we add up all the values for each unique microservice name
    df = df.groupby("microservice", as_index=False)["replicas"].sum()
    return df


if __name__ == "__main__":
    sla = 200
    starting_qps = 25
    current_qps = starting_qps
    try_again = True

    print("Starting with SLA:", sla)
    while(try_again):
        try_again = False
        # iterate from 25 to 700 in steps of 25
        for qps in range(current_qps, 701, 25):
            with open("./AE/scripts/configs/social-latency-psched-template.yaml", "r") as f:
                data = f.read()
                data = data.replace("!QPS!", f"{qps}")
                data = data.replace("!SLA!", f"{sla}")
            with open("./AE/scripts/configs/social-latency-psched.yaml", "w") as f:
                f.write(data)

            try:
                print("Generating values for qps:", qps)
                ms_replicas: pd.DataFrame = run_shell_commands()
            except subprocess.CalledProcessError as e:
                print(f"Error occurred while running shell commands for qps:{qps}, trying with higher SLA")
                print(e)
                # restart the qps loop starting from here with an increased SLA
                try_again = True
                current_qps = qps
                sla += 50
                print("New SLA:", sla)
                break

            print(ms_replicas)
            # open k8s_noservice.json as a json file and load it as a dictionary
            k8s_json: dict = None
            with open("k8s_noservice.json", "r") as f:
                k8s_json = json.loads(f.read())

            # parse the json file
            print(k8s_json.keys())

            for index, row in ms_replicas.iterrows():
                # create a new dict with the same keys as the json file
                new_dict = {
                    "name": row["microservice"],
                    "namespace": "socialnetwork",
                    "replicas": int(row["replicas"]),
                    "resources": {
                        "requests": {"cpu": "0.1", "memory": "200Mi"},
                        "limits": {"cpu": "0.1", "memory": "200Mi"},
                    },
                }
                # append the dict to the deployments key
                k8s_json["deployments"].append(new_dict)

            # write the json file to k8s.json
            with open(f"ERMS_Output_Configs/load_{qps}_config.json", "w") as f:
                f.write(json.dumps(k8s_json, indent=4))
    
