#!/usr/bin/env python


import k8s
import requests
import collections
import re
import functools
import os
import json
import importlib



###########################################################
##  Most Basic workflow

def func1(i):
    return -1 * i


def func2(i):
    return 10 * i


def basic_wf(i):
    a = k8s.wait(k8s.run(func1, i))
    b = k8s.wait(k8s.run(func2, a))
    return b


###########################################################
##  Count Words


document_url = "https://www.gutenberg.org/cache/epub/68283/pg68283.txt"
stopwords = importlib.resources.read_text("k8s", "stopwords.txt")


def chunk_list(lst, chunk_size):
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i+chunk_size]


def count_words(words, stopwords=stopwords):
    return collections.Counter([word for word in words if word not in stopwords])


def merge_counts(counts_list):
    from collections import Counter
    return functools.reduce(lambda a,b: Counter(a)+Counter(b), counts_list)


def count_words_workflow(url):
    text = requests.get(url).text.lower()
    words = re.split(r"\s+", text)

    count_jobs = k8s.map(count_words,
                         chunk_list(words, chunk_size=1000),
                         imports=["collections"],
                         nowait=True)

    merged_counts_job = k8s.run(lambda func=merge_counts, **kwargs: func(kwargs["inputs"].values()),
                                deps=count_jobs,
                                imports=["collections", "re", "functools"])

    #from IPython import embed; embed(header="inside_workflow")
    return collections.Counter(k8s.wait(merged_counts_job))



###########################################################
##  Fake NGS


def demux_batch(batch_folder):
    fastqs = [f"gs://{batch_folder}/sample-{i}.fq.gz" for i in range(3)]
    return fastqs


def align_bam(fastq):
    bam = re.sub(r"\.fq\.gz", ".bam", fastq)
    return bam


def sample_qc(bam):
    qc = re.sub(r"\.bam", ".qc.tsv", bam)
    return qc


def merge_qc(sample_qcs):
    qc = os.path.dirname(sample_qcs[0]) + "/basic_stats.tsv"
    return qc


def call_snps(bam):
    qc = re.sub(r"\.bam", ".vcf", bam)
    return qc


def ngs_workflow(batch_folder):
    def wf(batch_folder):
        import json, sys
        fastqs = k8s.wait(k8s.run(demux_batch, batch_folder))
        bams = k8s.map(align_bam, fastqs, imports=["re"])
        sample_qcs = k8s.map(sample_qc, bams, imports=["re"])
        snps = k8s.map(call_snps, bams, imports=["re"])
        basic_stats = k8s.wait(k8s.run(merge_qc, sample_qcs, imports=["os"]))
        return dict(fastq=fastqs,
                    bams=bams,
                    sample_qcs=sample_qcs,
                    snps=snps,
                    basic_stats=basic_stats)

    #print(k8s.run(wf, batch_folder, test=True))

    wf_job = k8s.run(wf, batch_folder)
    print("wf_job:", wf_job, flush=True)
    results = k8s.wait(wf_job)
    print(json.dumps(results, indent=4))
    return results


if __name__ == "__main__":
    #merged_counts = count_words_workflow(document_url)
    #with open("counts.txt", "w") as fp:
    #    for word, count in merged_counts.most_common():
    #        print(word, count, sep="\t", file=fp, flush=True)
    ngs_workflow("batch-1")

