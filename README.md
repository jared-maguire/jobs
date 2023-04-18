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

``` python
In [1]: import sk8s

In [2]: sk8s.run(lambda i, j: i+j, 2,3)
Out[2]: 'job-yealh'

In [3]: sk8s.wait('job-yealh')
Out[3]: 5

In [4]: sk8s.map(lambda i: 2*i, range(3))
Out[4]: [0, 2, 4]
```



``` python
def wf(batch_folder):
    import json, sys
    fastqs = sk8s.wait(k8s.run(demux_batch, batch_folder))
    bams = sk8s.map(align_bam, fastqs)
    sample_qcs = k8s.map(sample_qc, bams)
    snps = sk8s.map(call_snps, bams)
    basic_stats = sk8s.wait(k8s.run(merge_qc, sample_qcs))
    return dict(fastq=fastqs,
                bams=bams,
                sample_qcs=sample_qcs,
                snps=snps,
                basic_stats=basic_stats)

wf_job = sk8s.run(wf, batch_folder)
```

# Installation

First, have a k8s cluster configured. Docker Desktop can give you a single node k8s cluster with a few clicks. The examples here are all trivially small compute load, and should run locally just fine. The instructions below assume a local k8s cluster via Docker Desktop.

It's not on pypi yet, so...

```
# Get yourself a kubernetes cluster, then:

git clone git@github.com:jared-maguire/jobs.git
cd jobs
pip install .
docker build -t jobs .         # build the default docker image that jobs will run in
python -m sk8s config -apply   # create a service account on the cluster that allows jobs to submit other jobs
```


# Some cloud-specific notes:

## GKE

- Provisioning ReadWriteMany volumes on GKE is quite slow. Avoid it if you can.
