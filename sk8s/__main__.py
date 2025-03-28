import sk8s

import argparse
import importlib
import subprocess

import sk8s.dashboard


if __name__ == '__main__':
    parser = argparse.ArgumentParser("sk8s")
    subparsers = parser.add_subparsers(help="commands", dest="command")

    config = subparsers.add_parser('config-cluster', help='configure k8s cluster')
    config.add_argument('-dryrun', default=False, action='store_true', help='print the kubernetes yaml that will be applied')
    config.add_argument('-check', default=False, action='store_true', help='check that the cluster is properly configured')
    config.add_argument('-apply', default=False, action='store_true', help='apply the configuration to the cluster')

    config = subparsers.add_parser('config-local', help='configure sk8s for a local cluster')

    config = subparsers.add_parser('config-gke', help='configure sk8s for a GKE cluster')
    config.add_argument('-project', help='google cloud project name (required)', required=True)
    config.add_argument('-namespace', help='k8s namespace to use', required=True)

    config = subparsers.add_parser('config-eks', help='configure sk8s for a GKE cluster')
    config.add_argument('-account', help='eks account id (required)', required=True)
    config.add_argument('-region', help='eks region (required)', required=True)
    config.add_argument('-namespace', help='k8s namespace to use', required=True)

    config = subparsers.add_parser('containers', help='build worker container')
    config.add_argument('-branch', default="main", help='the tag we should use for the default jobs image')
    config.add_argument('-dev_mode', default=False, action="store_true", help='install the package from the current directory, rather than github')
    config.add_argument('-extra_options', default=" ", help='extra options for docker build')
    config.add_argument('-push', default=False, action='store_true', help="also push the image when it's built")

    config = subparsers.add_parser('clean_namespace', help='delete almost everything from the current k8s namespace')
    config.add_argument('-tag', default="jobs", help='the tag we should use for the default jobs image')
    config.add_argument('-push', default=False, action='store_true', help="also push the image when it's built")

    config = subparsers.add_parser('shell', help='launch an interactive shell')
    config.add_argument('-image', default=None, help='name of the image to use')
    config.add_argument('-volumes', default=[], nargs="+", help='names of any volumes to mount')
    config.add_argument('-privileged', default=False, action='store_true', help='use a privileged container')
    config.add_argument('-service_account_name', default=None, help='ServiceAccount to use')

    config = subparsers.add_parser('wait', help='wait for a job')
    config.add_argument('-job', required=True, help='name of the job to wait for')

    config = subparsers.add_parser('kubewatch', help='watch the cluster')
    config = subparsers.add_parser('dashboard', help='show cluster dashboard')

    #test = subparsers.add_parser('test', help='test k8s')
    #test.add_argument('-opt-one', action='store', help='option one')

    args = parser.parse_args()

    if args.command == "config-local":
        import sk8s.clouds.local
        sk8s.clouds.local.config_cluster()

    if args.command == "config-gke":
        import sk8s.clouds.gke
        sk8s.clouds.gke.config_cluster(project=args.project, namespace=args.namespace)

    if args.command == "config-eks":
        import sk8s.clouds.eks
        sk8s.clouds.eks.config_cluster(account=args.account, region=args.region, namespace=args.namespace)

    if args.command == "containers":
        ### Note: only works on local clusters right now
        sk8s.docker_build_jobs_image(branch=args.branch, dev_mode=args.dev_mode, extra_options=args.extra_options)

    if args.command == "clean_namespace":
        lines = subprocess.run("kubectl get all",
                                shell=True, check=True, stdout=subprocess.PIPE).stdout.decode("utf-8").split("\n")
        objects = [l.split()[0] for l in lines if (len(l.split()) > 2) and ("NAME" not in l)]
        objects_str = " ".join(objects)
        subprocess.run(f"kubectl delete {objects_str}", shell=True, check=True)

    if args.command == "wait":
        print(sk8s.wait(args.job))

    if args.command == "kubewatch":
        import time
        while(True):
            text = subprocess.run("kubectl get all", shell=True, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode("utf-8")
            text += "\n\n" + subprocess.run("kubectl get pvc", shell=True, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode("utf-8")

            import platform
            if "Windows" in platform.platform():
                subprocess.run("cls", shell=True, check=False)
            else:
                subprocess.run("clear", shell=True, check=False)

            print("⏹️  👀\n", flush=True)
            print(text, flush=True)
            time.sleep(1)

    if args.command == "shell":
        job = sk8s.util.interactive_job(image=args.image, volumes=args.volumes, privileged=args.privileged, service_account_name=args.service_account_name)

    if args.command == "dashboard":
        sk8s.dashboard.show_dashboard()
