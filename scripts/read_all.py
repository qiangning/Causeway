from itertools import chain

from causeway.because_data import CausalityStandoffReader
from nlpypline.data.io import DirectoryReader


def get_reader(recursive=False):
    return DirectoryReader((CausalityStandoffReader.FILE_PATTERN,),
                           CausalityStandoffReader(), recursive)

def read_all(datadir='/var/www/brat/data/finished', instances=True,
             overlapping=False, recursive=False):
    reader = get_reader(recursive)
    reader.open(datadir)
    if instances:
        all_sentences = chain.from_iterable(d.sentences for d in reader)
        if overlapping:
            all_instances = chain.from_iterable(s.overlapping_rel_instances
                                                for s in all_sentences)
        else:
            all_instances = chain.from_iterable(s.causation_instances
                                                for s in all_sentences)
        all_instances = list(all_instances)
        reader.close()
        return all_instances
    else:
        return reader.get_all()