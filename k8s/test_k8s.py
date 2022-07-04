#!/usr/bin/env python

import k8s


def test_run_and_wait():
    result = k8s.wait(k8s.run(lambda: "Hooray"), timeout=30)
    assert(result=="Hooray")


def test_map():
    results = k8s.map(lambda i: i*2, (0,1,2))
    assert(tuple(results) == (0,2,4))
