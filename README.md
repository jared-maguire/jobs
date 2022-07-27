# Run Python Functions on Kubernetes

Run many python functions as kubernetes jobs. Functions can run other functions as new jobs, and wait for their output, enabling complex workflows entirely in python.

## k8s.run(function, args, **kwargs)

Pickle `function` and `args`. 

Submit a kubernets job that (basically) unpickles and calls `print(json.dumps(function(*args)))` inside a kubernetes pod.

return the name of the kubernetes job that was submitted.

## k8s.wait(job)

Poll k8s until `job` is completed. Throw an exception if there was an error.

If job was successful, return `json.loads(subprocess.run(f'kubectl logs {job}', stdout=PIPE).stdout))`

In english, read whatever the job printed to stdout, decode it as json text, and return.

## Examples

``` python
In [1]: import k8s

In [2]: k8s.run(lambda i, j: i+j, 2,3)
Out[2]: 'job-yealh'

In [3]: k8s.wait('job-yealh')
Out[3]: 5

In [4]: k8s.map(lambda i: 2*i, range(3))
Out[4]: [0, 2, 4]
```



``` python
def wf(batch_folder):
    import json, sys
    fastqs = k8s.wait(k8s.run(demux_batch, batch_folder))
    bams = k8s.map(align_bam, fastqs)
    sample_qcs = k8s.map(sample_qc, bams)
    snps = k8s.map(call_snps, bams)
    basic_stats = k8s.wait(k8s.run(merge_qc, sample_qcs))
    return dict(fastq=fastqs,
                bams=bams,
                sample_qcs=sample_qcs,
                snps=snps,
                basic_stats=basic_stats)

wf_job = k8s.run(wf, batch_folder)
```

