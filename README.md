# Run Python Functions on Kubernetes

Run many python functions as kubernetes jobs. Functions can run other functions as new jobs, and wait for their output, enabling complex workflows entirely in python.

## sk8s.run(function, args, **kwargs)

Pickle `function` and `args`. 

Submit a kubernets job that (basically) unpickles and calls `print(json.dumps(function(*args)))` inside a kubernetes pod.

return the name of the kubernetes job that was submitted.

## sk8s.wait(job)

Poll k8s until `job` is completed. Throw an exception if there was an error.

If job was successful, return `json.loads(subprocess.run(f'kubectl logs {job}', stdout=PIPE).stdout))`

In english, read whatever the job printed to stdout, decode it as json text, and return.

## Examples

### Basics: run(), wait() and map()
These three functions are the foundation of most everything else.

``` python
In [1]: import sk8s

In [2]: sk8s.run(lambda i, j: i+j, 2,3)
Out[2]: 'job-yealh'

In [3]: sk8s.wait('job-yealh')
Out[3]: 5

In [4]: sk8s.map(lambda i: 2*i, range(3))
Out[4]: [0, 2, 4]
```

### Workflows
Jobs can submit other jobs, enabling fully-functional workflows.

``` python
In [1]: import sk8s

In [2]: def wf():
   ...:     step1_results = sk8s.map(lambda i: i, range(3))
   ...:     step2_result = sk8s.run(lambda inputs: sum(inputs), step1_results, asynchro=False)
   ...:     return step2_result
   ...:
   ...: sk8s.run(wf, asynchro=False, timeout=60)
Out[2]: 3
```

# Installation

## Local Cluster

First, have a k8s cluster configured. Docker Desktop can give you a single node k8s cluster with a few clicks. The examples here are all trivially small compute load, and should run locally just fine. The instructions below assume a local k8s cluster via Docker Desktop.

It's not on pypi yet, so...

```bash
# Get yourself a kubernetes cluster and a docker repo, then:

git clone git@github.com:jared-maguire/jobs.git
cd jobs
conda create -c conda-forge -n sk8s_test pip python=3.10  # make a python environment
conda activate sk8s_test                                  # activate the python environment
pip install -e .                                          # install this package in developer mode
python -m sk8s containers -tag master                     # build the "jobs" docker image
python -m sk8s config                                     # configure the k8s cluster (adds a service account)
pytest                                                    # run the tests!
```

This takes a little while. It's setting up a lot of things.

## Google

First, create a cluster and a container registry on Google Cloud. Configure `kubectl` and `docker` to use them.

I've found (as of april 2023) that a totally default GKE autopilot cluster works if you enable 'Filestore CSI driver' in the cluster config. You can do this via the google web ui.

Then:

```
python -m sk8s containers -tag master
python -m sk8s config-gke
```
