import k8s

import argparse
import importlib
import subprocess


if __name__ == '__main__':
    parser = argparse.ArgumentParser("k8s")
    subparsers = parser.add_subparsers(help="commands", dest="command")

    config = subparsers.add_parser('config', help='configure k8s cluster')
    config.add_argument('-dryrun', default=False, action='store_true', help='print the kubernetes yaml that will be applied')
    config.add_argument('-check', default=False, action='store_true', help='check that the cluster is properly configured')
    config.add_argument('-apply', default=False, action='store_true', help='apply the configuration to the cluster')

    config = subparsers.add_parser('containers', help='build worker container')
    config.add_argument('-tag', default="jobs", help='the tag we should use for the default jobs image')
    config.add_argument('-push', default=False, action='store_true', help="also push the image when it's built")

    #test = subparsers.add_parser('test', help='test k8s')
    #test.add_argument('-opt-one', action='store', help='option one')

    args = parser.parse_args()

    if args.command == "config":
        config = importlib.resources.read_text("k8s", "cluster_config.yaml")
        if args.dryrun:
            print(config)
        if args.check:
            print("Cluster configured:", k8s.check_cluster_config())
        if args.apply:
            subprocess.run("kubectl apply -f -", input=config.encode("utf-8"), check=True, shell=True) 
    if args.command == "containers":
        raise NotImplementedError("container setup not implemented yet")