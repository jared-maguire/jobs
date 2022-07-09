# Run Python Functions on Kubernetes

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


