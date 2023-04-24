#!/usr/bin/env python

### ⚠️ This little helper script is very specific to Jared's cluster situation.

import subprocess
import argparse
import sk8s


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["gke", "gke2", "local"])
    parser.add_argument("-container", default=False, action="store_true")
    args = parser.parse_args()

    print(args.cmd)

    if args.cmd == "gke":
        subprocess.run("kubectl config use-context gke_jared-genome-analysis_us-central1_autopilot-cluster-1", check=True, shell=True)
        subprocess.run("python -m sk8s config-gke -project jared-genome-analysis", check=True, shell=True)

    if args.cmd == "gke2":
        subprocess.run("kubectl config use-context gke_jared-genome-analysis_us-central1_autopilot-cluster-2", check=True, shell=True)
        subprocess.run("python -m sk8s config-gke -project jared-genome-analysis", check=True, shell=True)

    if args.cmd == "local":
        subprocess.run("kubectl config use-context docker-desktop", check=True, shell=True)
        subprocess.run("python -m sk8s config-local", check=True, shell=True)

    print(sk8s.configs.load_config())

    if args.container:
        subprocess.run("python -m sk8s containers", check=True, shell=True)