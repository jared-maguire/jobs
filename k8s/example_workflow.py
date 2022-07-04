#!/usr/bin/env python


import k8s
import requests
import collections
import re
import functools


document_url = "https://www.gutenberg.org/cache/epub/68283/pg68283.txt"


def chunk_list(lst, chunk_size):
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i+chunk_size]


def count_words(words):
    return collections.Counter(words)


def merge_counts(counts_list):
    return functools.reduce(lambda a,b: a+b, counts_list)


def count_words_workflow(url):
    text = requests.get(url).text
    words = re.split("\s+", text)

    count_jobs = k8s.map(count_words,
                         chunk_list(words, chunk_size=1000),
                         imports=["collections"],
                         nowait=True)

    merged_counts_job = k8s.run(lambda func=merge_counts, **kwargs: func(kwargs["inputs"]),
                                deps=count_jobs,
                                imports=["collections", "re", "functools"])

    from IPython import embed; embed(header="inside_workflow")
    return collections.Counter(k8s.wait(merged_counts_job))


if __name__ == "__main__":

    merged_counts = count_words_workflow(document_url)

    from IPython import embed; embed()

    with open("counts.txt", "w") as fp:
        for word, count in merged_counts.most_common():
            print(word, count, sep="\t", file=fp)

