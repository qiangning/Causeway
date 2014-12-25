from gflags import DEFINE_string, FLAGS, DuplicateFlagError
import threading
import logging
import subprocess
import tempfile
import time

from data import ParsedSentence
from util.metrics import ClassificationMetrics
from pipeline import Stage
from pipeline.models import Model
from stages import match_causation_pairs, print_instances_by_eval_result, normalize_order

try:
    DEFINE_string('tregex_command',
                  '/home/jesse/Documents/Work/Research/'
                  'stanford-tregex-2014-10-26/tregex.sh',
                  'Command to run TRegex')
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)


class PossibleCausation(object):
    def __init__(self, arg1, arg2, matching_pattern, correct):
        self.arg1 = arg1
        self.arg2 = arg2
        self.matching_pattern = matching_pattern
        self.correct = correct

class ConnectiveModel(Model):
    def __init__(self, *args, **kwargs):
        super(ConnectiveModel, self).__init__(*args, **kwargs)
        self.tregex_patterns = []

    @staticmethod
    def get_pattern_for_arg(connective, arg, arg_name):
        parent_sentence = connective.parent_sentence
        dep_path = parent_sentence.extract_dependency_path(connective, arg)
        pattern = '=connective'
        last_node = connective

        for source, target, dep_name in dep_path:
            forward_dependency = source is last_node
            if forward_dependency:
                next_node = target
            else:
                next_node = source

            if next_node is arg:
                node_name = '=' + arg_name
            else:
                node_name = ''

            if parent_sentence.is_clause_head(next_node):
                node_pos_pattern = '[<2 /^VB.*/ | < (__ <1 cop)]'
            else:
                node_pos_pattern = ('<2 /^%s.*/' % next_node.get_gen_pos())

            if forward_dependency:
                pattern = '%s < (__%s %s <1 %s' % (
                    pattern, node_name, node_pos_pattern, dep_name)
            else:
                pattern = '%s <1 %s > (__%s %s' % (
                    pattern, dep_name, node_name, node_pos_pattern)

            last_node = next_node

        pattern += ')' * len(dep_path)
        return pattern

    def _extract_patterns(self, sentences):
        # TODO: Extend this code to multiple-word connectives/args.
        # TODO: Figure out tree transformations to get rid of dumb things like
        # conjunctions that introduce spurious differences btw tregex_patterns?
        tregex_patterns = set()
        for sentence in sentences:
            for instance in sentence.causation_instances:
                if (len(instance.connective) == 1 and instance.cause != None
                    and instance.effect is not None):
                    connective = instance.connective[0]
                    cause_head = sentence.get_head(instance.cause)
                    effect_head = sentence.get_head(instance.effect)

                    cause_pattern = (self.get_pattern_for_arg(
                        connective, cause_head, 'cause'))
                    effect_pattern = (self.get_pattern_for_arg(
                        connective, effect_head, 'effect'))
                    connective_pattern = (
                        '/^%s_[0-9]+$/=connective <2 /^%s.*/' % (
                            connective.lemma, connective.get_gen_pos()))
                    pattern = '%s : %s : %s' % (
                        connective_pattern, cause_pattern, effect_pattern)

                    #if pattern not in tregex_patterns:
                    #    logging.debug(
                    #        'Adding pattern:\n\t%s\n\tSentence: %s\n'
                    #        % (pattern, sentence.original_text))
                    tregex_patterns.add(pattern)

        self.tregex_patterns = list(tregex_patterns)

    class TregexProcessorThread(threading.Thread):
        def __init__(self, pattern, trees_file_path, sentences,
                     true_causation_pairs_by_sentence, *args, **kwargs):
            super(ConnectiveModel.TregexProcessorThread, self).__init__(
                *args, **kwargs)
            self.pattern = pattern
            self.progress = 0
            self.trees_file_path = trees_file_path
            self.sentences = sentences
            self.true_causation_pairs_by_sentence = (
                true_causation_pairs_by_sentence)

        dev_null = open('/dev/null', 'w')
        tregex_args = '-u -s -o -l -N -h cause -h effect'.split()

        def run(self):
            # Create input and output files
            with tempfile.NamedTemporaryFile('w+b') as tregex_output:
                full_tregex_command = (
                    [FLAGS.tregex_command] + self.tregex_args
                    + [self.pattern, self.trees_file_path])
                subprocess.call(full_tregex_command, stdout=tregex_output,
                                stderr=self.dev_null)
                self.progress += 2 * len(self.sentences)
                tregex_output.seek(0)

                # For each sentence, we leave the file positioned at the next
                # tree number line.
                for sentence, true_causation_pairs in zip(
                    self.sentences, self.true_causation_pairs_by_sentence):
                    # Read TRegex output for the sentence.
                    tregex_output.readline() # skip tree num line
                    next_line = tregex_output.readline().strip()
                    lines = []
                    while next_line:
                        lines.append(next_line)
                        next_line = tregex_output.readline().strip()

                    # Parse TRegex output.
                    line_pairs = zip(lines[0::2], lines[1::2])
                    for line_pair in line_pairs:
                        index_pair = [int(line.split("_")[-1])
                                      for line in line_pair]
                        index_pair = tuple(sorted(index_pair))

                        # Mark sentence if possible connective is present.
                        t1_index, t2_index = index_pair
                        in_gold = index_pair in true_causation_pairs
                        possible = PossibleCausation(
                            sentence.tokens[t1_index],
                            sentence.tokens[t2_index], self.pattern, in_gold)

                        # THIS IS THE ONLY LINE THAT MUTATES SHARED DATA.
                        # It is thread-safe, because lists are thread-safe, and
                        # we never reassign sentence.possible_causations.
                        sentence.possible_causations.append(possible)

                    self.progress += 1

    def train(self, sentences):
        self._extract_patterns(sentences)
        # Now that we have all the patterns, we also need to make sure all the
        # instances we pass along to the next stage have input matching what
        # will be passed along at test time. That means we need false negatives
        # in exactly the same places that they'll be at test time, so we just
        # run test() to find all the correct and spurious matches.
        logging.debug("Running test to generate input for next stage")
        self.test(sentences)

    def test(self, sentences):
        logging.info('Tagging possible connectives...')
        # Interacting with the TRegex processes is heavily I/O-bound, so we use
        # threads to parallelize it a bit -- one thread per TRegex.

        # First, do sentence pre-processing that's common across all TRegexes.
        ptb_strings = []
        true_causation_pairs_by_sentence = []
        for sentence in sentences:
            sentence.possible_causations = []
            # Add newlines for writing to file later.
            ptb_strings.append(sentence.to_ptb_tree_string() + '\n')
            true_causation_pairs = [
                normalize_order(instance.get_cause_and_effect_heads())
                for instance in sentence.causation_instances]
            true_causation_pairs_by_sentence.append(set(
                [(arg_1.index, arg_2.index)
                 for arg_1, arg_2 in true_causation_pairs
                 if arg_1 is not None and arg_2 is not None]))

        # Set up progress reporter
        threads = []
        all_threads_done = False
        def report_progress_repeatedly():
            while(True):
                time.sleep(3)
                # Each thread gets 3 * len(sentences) of progress points: two
                # rounds for executing the TRegex processes and one for
                # processing the results.
                # This will be a slightly imprecise estimate, because progress
                # numbers are being grabbed in a non-threadsafe way. Whatever.
                progress = (sum([t.progress for t in threads])
                            / float(len(threads) * 3 * len(sentences)))
                if not all_threads_done:
                    logging.info("Tagging connectives: %1.0f%% complete"
                                 % (progress * 100))
                else:
                    break
        progress_reporter = threading.Thread(target=report_progress_repeatedly)
        progress_reporter.daemon = True

        try:
            progress_reporter.start()

            with tempfile.NamedTemporaryFile('w') as trees_file:
                trees_file.writelines(ptb_strings)

                # Start the threads.
                for pattern in self.tregex_patterns:
                    new_thread = self.TregexProcessorThread(
                        pattern, trees_file.name, sentences,
                        true_causation_pairs_by_sentence)
                    threads.append(new_thread)
                    new_thread.start()
                for thread in threads:
                    thread.join()
        finally:
            # Make sure progress reporter exits
            all_threads_done = True

        logging.info("Done tagging possible connectives.")

class ConnectiveStage(Stage):
    def __init__(self, name):
        super(ConnectiveStage, self).__init__(
            name, [ConnectiveModel(part_type=ParsedSentence)])

    def get_produced_attributes(self):
        return ['possible_causations']

    def _extract_parts(self, sentence):
        return [sentence]

    def _begin_evaluation(self):
        self.tp, self.fp, self.fn = 0, 0, 0
        if FLAGS.sc_print_test_instances:
            self.tp_pairs, self.fp_pairs, self.fn_pairs = [], [], []

    def _evaluate(self, sentences):
        for sentence in sentences:
            predicted_pairs = [(pc.arg1, pc.arg2)
                               for pc in sentence.possible_causations]
            expected_pairs = [i.get_cause_and_effect_heads()
                              for i in sentence.causation_instances]
            tp, fp, fn = match_causation_pairs(
                expected_pairs, predicted_pairs, self.tp_pairs, self.fp_pairs,
                self.fn_pairs)

            self.tp += tp
            self.fp += fp
            self.fn += fn

    def _complete_evaluation(self):
        results = ClassificationMetrics(self.tp, self.fp, self.fn, None)
        if FLAGS.sc_print_test_instances:
            print_instances_by_eval_result(self.tp_pairs, self.fp_pairs,
                                           self.fn_pairs)
            self.tp_pairs, self.fp_pairs, self.fn_pairs = [], [], []
        return results