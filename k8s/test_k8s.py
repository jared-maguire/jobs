#!/usr/bin/env python

import k8s


def test_run_and_wait():
    result = k8s.wait(k8s.run(lambda: "Hooray"), timeout=30)
    assert(result=="Hooray")


def test_map():
    results = k8s.map(lambda i: i*2, (0,1,2))
    assert(tuple(results) == (0,2,4))


def test_deps():
    job1 = k8s.run(lambda: "job-1", deps=[])
    job2 = k8s.run(lambda j=job1, **kwargs: "job-2 " + kwargs["inputs"][j], deps=[job1])
    result = k8s.wait(job2, timeout=30)
    # clean up job1
    k8s.wait(job1, timeout=30)
    assert(result == "job-2 job-1")


def test_simple_workflow():
    jobs1 = k8s.map(lambda i: i, range(3), nowait=True)
    jobs2 = k8s.map(lambda i, **kwargs: sum(kwargs["inputs"].values()), range(3), deps=jobs1, nowait=True)
    results = [k8s.wait(j, timeout=30) for j in jobs2]
    # clean up jobs1
    _ = [k8s.wait(j, timeout=30) for j in jobs1]
    assert(tuple(results) == (3, 3, 3))


def test_ngs_workflow():
    from example_workflow import ngs_workflow
    results = ngs_workflow("batch-1")
    assert(results.__class__ == dict)
