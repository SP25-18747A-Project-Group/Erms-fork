import subprocess
import pandas as pd
import io
import json


def run_shell_commands():
    try:
        # Run the first shell command
        subprocess.run(
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
        )

        # Run the second shell command
        subprocess.run(
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
            row = [row[1], int(row[3])]
            # append to dataframe
            df = pd.concat(
                [pd.DataFrame([[row[0], row[1]]], columns=df.columns), df],
                ignore_index=True,
            )

        # do a sum on the replicas column so that we add up all the values for each unique microservice name
        df = df.groupby("microservice", as_index=False)['replicas'].sum()
        return df

    except subprocess.CalledProcessError as e:
        print(f"An error occurred while running a command: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    ms_replicas: pd.DataFrame = run_shell_commands()
    print(ms_replicas)
    # open k8s_noservice.json as a json file and load it as a dictionary
    k8s_json:dict = None
    with open("k8s_noservice.json", "r") as f:
        k8s_json = json.loads(f.read())
    
    # parse the json file
    print(k8s_json.keys())


    #{
    #     "name": "compose-post-service",
    #     "namespace": "socialnetwork",
    #     "replicas": 1,
    #     "resources": {
    #       "requests": {
    #         "cpu": "2",
    #         "memory": "2.0Gi"
    #       },
    #       "limits": {
    #         "cpu": "2",
    #         "memory": "2.0Gi"
    #       }
    #     }
    #   }
    # add the microservices and replicas to the json file under the "deployments" key (which has an array as it's value)

    for index, row in ms_replicas.iterrows():
        # create a new dict with the same keys as the json file
        new_dict = {
            "name": row["microservice"],
            "namespace": "socialnetwork",
            "replicas": int(row["replicas"]),
            "resources": {
                "requests": {
                    "cpu": "2",
                    "memory": "2.0Gi"
                },
                "limits": {
                    "cpu": "2",
                    "memory": "2.0Gi"
                }
            }
        }
        # append the dict to the deployments key
        k8s_json["deployments"].append(new_dict)

    # write the json file to k8s.json
    with open("k8s.json", "w") as f:
        f.write(json.dumps(k8s_json, indent=4))
