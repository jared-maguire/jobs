def read_fq_record(fp):
        name=fp.readline()
        if name == "":
            # EOF
            return None

        record = dict(
            name=name.strip(),
            seq=fp.readline().strip(),
            plus=fp.readline().strip(),
            qual=fp.readline().strip(),
            )
        return record 


def fq_iter(fq):
    with open (fq) as fp:
        record = read_fq_record(fp)
        while record is not None:
            yield record
            record = read_fq_record(fp)


def split_fastq(input_fq1, input_fq2, output_prefix, n_chunks):
    fq1 = fq_iter(input_fq1)
    fq2 = fq_iter(input_fq2)

    output_filenames = [f"{output_prefix}chunk.{i}" for i in range(n_chunks)]
    output_fps = [open(f, "w") for f in output_filenames]

    a = next(fq1)
    b = next(fq2)
    current = 0
    while ((a is not None) and (b is not None)):
        if (b is None) or a["name"] < b["name"]:
            print(a["name"], a["seq"], a["plus"], a["qual"],
                  sep="\n", file=output_fps[current])
            a = next(fq1, None)
        elif (a is None) or (a["name"] > b["name"]):
            print(b["name"], b["seq"], b["plus"], b["qual"],
                  sep="\n", file=output_fps[current])
            b = next(fq2, None)
        else:
            print(a["name"], a["seq"], a["plus"], a["qual"],
                  sep="\n", file=output_fps[current])
            print(b["name"], b["seq"], b["plus"], b["qual"],
                  sep="\n", file=output_fps[current])
            a = next(fq1, None)
            b = next(fq2, None)
        current = (current + 1) % n_chunks

    for fp in output_fps:
        fp.close()