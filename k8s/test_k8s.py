#!/usr/bin/env python

import k8s
import numpy


def test_run_and_wait():
    result = k8s.wait(k8s.run(lambda: "Hooray"), timeout=30)
    assert(result=="Hooray")


def test_map():
    input = numpy.array(range(10))
    expected_output = input * 2
    results = k8s.map(lambda i: i*2, input)
    assert(results == expected_output)
