#!/usr/bin/env python

import k8s


def test_cluster_config():
    assert(k8s.check_cluster_config())


def test_run_and_wait():
    result = k8s.wait(k8s.run(lambda: "Hooray"), timeout=30)
    assert(result=="Hooray")


def test_fail():
    import os
    try:
        job = k8s.run(lambda: 1/0)
        k8s.wait(job, timeout=30)
    except RuntimeError:
        assert(os.system(f"kubectl delete job {job}") == 0)
        return
    assert(False)


def test_map():
    results = k8s.map(lambda i: i*2, (0,1,2))
    assert(tuple(results) == (0,2,4))


def test_deps():
    job1 = k8s.run(lambda: "job-1")
    job2 = k8s.run(lambda result=k8s.wait(job1): "job-2 " + result) 
    result = k8s.wait(job2, timeout=30)
    assert(result == "job-2 job-1")


def test_simple_workflow():
    def wf():
        jobs1 = k8s.map(lambda i: i, range(3), nowait=True)
        return k8s.wait(k8s.run(lambda inputs: sum(inputs), map(k8s.wait, jobs1)), timeout=30)
    result = k8s.wait(k8s.run(wf), timeout=60)
    assert(result == 3)


def test_nested_lambda():
    job = k8s.run(lambda i, j: 10 * k8s.wait(k8s.run(lambda a=i, b=j: a+b)), 1,2)
    result = k8s.wait(job)
    assert(result == 30)


def test_ngs_workflow():
    from example_workflow import ngs_workflow
    results = ngs_workflow("batch-1")
    assert(results.__class__ == dict)


def test_count_words_workflow():
    import collections
    from example_workflow import count_words_workflow, document_url
    results = count_words_workflow(document_url)
    assert(results.__class__ == collections.Counter)