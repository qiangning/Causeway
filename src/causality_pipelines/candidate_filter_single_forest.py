from collections import defaultdict
from gflags import (DEFINE_list, DEFINE_integer, DEFINE_bool, FLAGS,
                    DuplicateFlagError)
import itertools
import logging
from nltk.corpus import wordnet
from scipy.spatial import distance

from causality_pipelines import IAAEvaluator, StanfordNERStage
from data import Token, StanfordParsedSentence, CausationInstance
from iaa import make_annotation_comparator
from nlp.senna import SennaEmbeddings
import numpy as np
from pipeline import Stage
from pipeline.featurization import (
    KnownValuesFeatureExtractor, FeatureExtractor, SetValuedFeatureExtractor,
    VectorValuedFeatureExtractor)
from pipeline.models import ClassifierModel
from pipeline.models.structured import StructuredDecoder, StructuredModel
from util.diff import SequenceDiff


try:
    DEFINE_list(
        'causality_cc_features',
        'cause_pos,effect_pos,wordsbtw,deppath,deplen,connective,cn_lemmas,'
        'tenses,cause_case_children,effect_case_children,domination,'
        'vector_dist,vector_cos_dist'.split(','),
        'Features to use for pattern-based candidate classifier model')
    DEFINE_integer('causality_cc_max_wordsbtw', 10,
                   "Maximum number of words between phrases before just making"
                   " the value the max")
    DEFINE_integer('causality_cc_max_dep_path_len', 3,
                   "Maximum number of dependency path steps to allow before"
                   " just making the value 'LONG-RANGE'")
    DEFINE_bool('causality_cc_print_test_instances', False,
                'Whether to print differing IAA results during evaluation')
    DEFINE_bool('causality_cc_diff_correctness', None,
                'Whether a candidate instance should be considered correct in'
                ' training based on diffing the sequence of true instances and'
                ' the sequence of proposed instances. If False, then any'
                ' proposed instance that has been matched to a true instance'
                " will be marked as true, even if it's a duplicate. Default is"
                " True for regex pipeline and False for TRegex.")
    DEFINE_bool('causality_cc_train_with_partials', False,
                'Whether to train the candidate classifier model counting'
                ' partial overlap as correct')
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)


class PatternFilterPart(object):
    def __init__(self, possible_causation, connective_correct=None):
        self.possible_causation = possible_causation
        self.sentence = possible_causation.sentence
        self.cause = possible_causation.cause
        self.effect = possible_causation.effect
        self.cause_head = self.sentence.get_head(possible_causation.cause)
        self.effect_head = self.sentence.get_head(possible_causation.effect)
        self.connective = possible_causation.connective
        self.connective_head = self.sentence.get_head(self.connective)
        self.connective_patterns = possible_causation.matching_patterns
        self.connective_correct = connective_correct


# In principle, the causation filter is both a featurized classifier and a
# structured model. (It is structured in that we get a list of predicted
# instances for each sentence, and then we want to choose the best list for the
# entire sentence.) In practice, it is easier to describe the classifier through
# composition rather than inheritance.
# TODO: is this still true?

class CausalClassifierModel(ClassifierModel):
    def __init__(self, classifier, selected_features=None,
        model_path=None, save_featurized=False):
        super(CausalClassifierModel, self).__init__(
            self, classifier=classifier, selected_features=selected_features,
            model_path=model_path, save_featurized=save_featurized)

    def _get_gold_labels(self, classifier_parts):
        return [part.connective_correct for part in classifier_parts]

    #############################
    # Feature extraction methods
    #############################

    @staticmethod
    def words_btw_heads(part):
        words_btw = part.sentence.count_words_between(
            part.cause_head, part.effect_head)
        return min(words_btw, FLAGS.causality_cc_max_wordsbtw)

    @staticmethod
    def extract_dep_path(part):
        deps = part.sentence.extract_dependency_path(
            part.cause_head, part.effect_head, False)
        if len(deps) > FLAGS.causality_cc_max_dep_path_len:
            return 'LONG-RANGE'
        else:
            return str(deps)

    # We're going to be extracting tenses for pairs of heads for the same
    # sentence. That means we'll get calls for the same head repeatedly, so we
    # cache them for as long as we're dealing with the same sentence.
    # TODO: Make framework send "done training" or "done testing" signals to
    # tell classifier to clear caches.
    __cached_tenses = {}
    __cached_tenses_sentence = None
    @staticmethod
    def extract_tense(head):
        if head.parent_sentence is (
            CausalClassifierModel.__cached_tenses_sentence):
            try:
                return CausalClassifierModel.__cached_tenses[head]
            except KeyError:
                pass
        else:
            CausalClassifierModel.__cached_tenses_sentence = (
                head.parent_sentence)
            CausalClassifierModel.__cached_tenses = {}

        tense = head.parent_sentence.get_auxiliaries_string(head)
        CausalClassifierModel.__cached_tenses[head] = tense
        return tense

    @staticmethod
    def extract_daughter_deps(part):
        sentence = part.sentence
        deps = sentence.get_children(part.connective_head)
        edge_labels = [label for label, _ in deps]
        edge_labels.sort()
        return tuple(edge_labels)

    @staticmethod
    def extract_incoming_dep(part):
        edge_label, _parent = part.sentence.get_most_direct_parent(
            part.connective_head)
        return edge_label

    @staticmethod
    def get_verb_children_deps(part):
        if part.connective_head.pos not in Token.VERB_TAGS:
            return 'Non-verb'

        sentence = part.sentence
        children = [child for _, child in
                    sentence.get_children(part.connective_head)]
        verb_children_deps = set()
        for child in children:
            child_deps = [dep for dep, _ in sentence.get_children(child)]
            verb_children_deps.update(child_deps)

        return tuple(verb_children_deps)

    @staticmethod
    def extract_parent_pos(part):
        return part.sentence.get_most_direct_parent(part.connective_head)[1].pos

    _ALL_POS_PAIRS = ['/'.join(tags) for tags in itertools.product(
                        Token.ALL_POS_TAGS, Token.ALL_POS_TAGS)]
    @staticmethod
    def extract_pos_bigram(part, argument_head):
        if argument_head.index < 2:
            prev_pos = 'NONE'
        else:
            previous_token = part.sentence.tokens[argument_head.index - 1]
            # TODO: would this be helpful or harmful?
            # while previous_token.pos in Token.PUNCT_TAGS:
            #     previous_token = part.sentence.tokens[
            #         previous_token.index - 1]
            prev_pos = previous_token.pos
        '/'.join([prev_pos, argument_head.pos])

    @staticmethod
    def extract_wn_hypernyms(token):
        ''' Extracts all Wordnet hypernyms, including the token's lemma. '''
        wn_pos_key = token.get_gen_pos()[0].lower()
        if wn_pos_key == 'j': # correct adjective tag for Wordnet
            wn_pos_key = 'a'
        try:
            synsets = wordnet.synsets(token.lemma, pos=wn_pos_key)
        except KeyError: # Invalid POS tag
            return []
        
        synsets_with_hypernyms = set()
        for synset in synsets:
            for hypernym_path in synset.hypernym_paths():
                synsets_with_hypernyms.update(hypernym_path)

        return tuple(synset.name() for synset in synsets_with_hypernyms)

    @staticmethod
    def extract_case_children(arg_head):
        child_tokens = arg_head.parent_sentence.get_children(arg_head, 'case')
        child_tokens.sort(key=lambda token: token.index)
        return ' '.join([token.lemma for token in child_tokens])

    _embeddings = None # only initialize if being used
    @staticmethod
    def extract_vector(arg_head):
        if not CausalClassifierModel._embeddings:
            CausalClassifierModel._embeddings = SennaEmbeddings()
        try:
            return CausalClassifierModel._embeddings[arg_head.lowered_text]
        except KeyError: # Unknown word; return special vector
            return CausalClassifierModel._embeddings['UNKNOWN']

    @staticmethod
    def extract_vector_dist(head1, head2):
        v1 = CausalClassifierModel.extract_vector(head1)
        v2 = CausalClassifierModel.extract_vector(head2)
        return np.linalg.norm(v1 - v2)

    @staticmethod
    def extract_vector_cos_dist(head1, head2):
        v1 = CausalClassifierModel.extract_vector(head1)
        v2 = CausalClassifierModel.extract_vector(head2)
        return distance.cosine(v1, v2)

    all_feature_extractors = []


CausalClassifierModel.all_feature_extractors = [
    KnownValuesFeatureExtractor('cause_pos', lambda part: part.cause_head.pos,
                                Token.ALL_POS_TAGS),
    KnownValuesFeatureExtractor('effect_pos', lambda part: part.effect_head.pos,
                                Token.ALL_POS_TAGS),
    KnownValuesFeatureExtractor('pos_pair', lambda part: '/'.join([
                                    part.cause_head.pos, part.effect_head.pos]),
                                CausalClassifierModel._ALL_POS_PAIRS),
    KnownValuesFeatureExtractor(
        'cause_pos_bigram',
        lambda part: CausalClassifierModel.extract_pos_bigram(
                             part, part.cause_head),
                         CausalClassifierModel._ALL_POS_PAIRS),
    KnownValuesFeatureExtractor(
        'effect_pos_bigram',
        lambda part: CausalClassifierModel.extract_pos_bigram(
                             part, part.effect_head),
                         CausalClassifierModel._ALL_POS_PAIRS),
    # Generalized POS tags don't seem to be that useful.
    KnownValuesFeatureExtractor(
        'cause_pos_gen', lambda part: part.cause_head.get_gen_pos(),
        Token.ALL_POS_TAGS),
    KnownValuesFeatureExtractor(
        'effect_pos_gen', lambda part: part.effect_head.get_gen_pos(),
        Token.ALL_POS_TAGS),
    FeatureExtractor('wordsbtw', CausalClassifierModel.words_btw_heads,
                     FeatureExtractor.FeatureTypes.Numerical),
    FeatureExtractor('deppath', CausalClassifierModel.extract_dep_path),
    FeatureExtractor('deplen',
                     lambda part: len(part.sentence.extract_dependency_path(
                        part.cause_head, part.effect_head)),
                     FeatureExtractor.FeatureTypes.Numerical),
    SetValuedFeatureExtractor(
        'connective', lambda part: part.connective_patterns),
    FeatureExtractor('tenses',
                     lambda part: '/'.join(
                        [CausalClassifierModel.extract_tense(head)
                         for head in part.cause_head, part.effect_head])),
    FeatureExtractor('cn_daughter_deps',
                     CausalClassifierModel.extract_daughter_deps),
    FeatureExtractor('cn_incoming_dep',
                     CausalClassifierModel.extract_incoming_dep),
    FeatureExtractor('verb_children_deps',
                     CausalClassifierModel.get_verb_children_deps),
    FeatureExtractor('cn_parent_pos',
                     CausalClassifierModel.extract_parent_pos),
    FeatureExtractor('cn_words',
                     lambda part: ' '.join([t.lowered_text
                                            for t in part.connective])),
    FeatureExtractor('cn_lemmas',
                     lambda part: ' '.join([t.lemma
                                            for t in part.connective])),
    SetValuedFeatureExtractor(
        'cause_hypernyms',
        lambda part: CausalClassifierModel.extract_wn_hypernyms(
            part.cause_head)),
    SetValuedFeatureExtractor(
        'effect_hypernyms',
        lambda part: CausalClassifierModel.extract_wn_hypernyms(
            part.effect_head)),
    FeatureExtractor(
        'cause_case_children',
        lambda part: CausalClassifierModel.extract_case_children(
                        part.cause_head)),
    FeatureExtractor('effect_case_children',
        lambda part: CausalClassifierModel.extract_case_children(
                        part.effect_head)),
    KnownValuesFeatureExtractor('domination',
        lambda part: part.sentence.get_domination_relation(
        part.cause_head, part.effect_head),
        range(len(StanfordParsedSentence.DOMINATION_DIRECTION))),
    VectorValuedFeatureExtractor(
        'cause_vector',
        lambda part: CausalClassifierModel.extract_vector(
                        part.cause_head)),
    VectorValuedFeatureExtractor(
        'effect_vector',
        lambda part: CausalClassifierModel.extract_vector(
                        part.cause_head)),
    FeatureExtractor(
        'vector_dist',
        lambda part: CausalClassifierModel.extract_vector_dist(
                         part.cause_head, part.effect_head),
        FeatureExtractor.FeatureTypes.Numerical),
    FeatureExtractor(
        'vector_cos_dist',
        lambda part: CausalClassifierModel.extract_vector_cos_dist(
                        part.cause_head, part.effect_head),
        FeatureExtractor.FeatureTypes.Numerical),
    KnownValuesFeatureExtractor(
        'ners', lambda part: '/'.join(
                    StanfordNERStage.NER_TYPES[arg_head.ner_tag]
                    for arg_head in [part.cause_head, part.effect_head]),
        StanfordNERStage.NER_TYPES)
]


class PatternBasedCausationFilter(StructuredModel):
    def __init__(self, classifier, save_featurized=False):
        super(PatternBasedCausationFilter, self).__init__(
            PatternBasedFilterDecoder())
        self.classifier = CausalClassifierModel(
            classifier, FLAGS.causality_cc_features,
            save_featurized=save_featurized)
        comparator = make_annotation_comparator(
            FLAGS.causality_cc_train_with_partials)
        # Comparator for matching CausationInstances against PossibleCausations
        self.connective_comparator = lambda inst1, inst2: comparator(
                                        inst1.connective, inst2.connective)
        # By default, regex, not tregex, should use diff correctness.
        if FLAGS.causality_cc_diff_correctness is None:
            FLAGS.causality_cc_diff_correctness = (
                'tregex' not in FLAGS.pipeline_type)
            logging.debug("Set flag causality_cc_diff_correctness to %s"
                          % FLAGS.causality_cc_diff_correctness)

    def _make_parts(self, sentence, is_train):
        if is_train:
            if FLAGS.causality_cc_diff_correctness:
                # In training, we need to match the causation instances the
                # pipeline has thus far detected against the original causation
                # instances (provided by previous pipeline stages). We do this
                # the same way that the IAA code does it internally: by running
                # a diff on the connectives. Except we cheat a bit, and compare
                # PossibleCausations against real CausationInstances.
                parts = []
                # We want the diff to sort by connective position.
                sort_by_key = lambda inst: inst.connective[0].start_offset
                connectives_diff = SequenceDiff(
                    sentence.possible_causations, sentence.causation_instances,
                    self.connective_comparator, sort_by_key)
                for correct_pc, _ in connectives_diff.get_matching_pairs():
                    if correct_pc.cause and correct_pc.effect:
                        parts.append(PatternFilterPart(correct_pc, True))
                for incorrect_pc in connectives_diff.get_a_only_elements():
                    if incorrect_pc.cause and incorrect_pc.effect:
                        parts.append(PatternFilterPart(incorrect_pc, False))
                return parts
            else:
                return [PatternFilterPart(pc, bool(pc.true_causation_instance))
                        for pc in sentence.possible_causations
                        if pc.cause and pc.effect]
        else:
            # If we're not in training, the initial label doesn't really matter.
            return [PatternFilterPart(pc, False) for pc in
                    sentence.possible_causations if pc.cause and pc.effect]

    def _train_structured(self, instances, parts_by_instance):
        self.classifier.train(list(itertools.chain(*parts_by_instance)))

    def _score_parts(self, instance, instance_parts):
        if instance_parts:
            # Return array of classification results by part.
            return self.classifier.test(instance_parts)
        else:
            return []


class PatternBasedFilterDecoder(StructuredDecoder):
    def decode(self, sentence, classifier_parts, labels):
        # Deduplicate the results.

        tokens_to_parts = defaultdict(int)
        positive_parts = [part for part, label in zip(classifier_parts, labels)
                          if label]
        for part in positive_parts:
            # Count every instance each connective word is part of.
            for connective_token in part.connective:
                tokens_to_parts[connective_token] += 1

        causation_instances = []
        for part in positive_parts:
            keep_part = True
            for token in part.connective:
                if tokens_to_parts[token] > 1:
                    # Assume that if there are other matches for a word, and
                    # this match relies on Steiner nodes, it's probably wrong.
                    # TODO: should we worry about cases where all connectives
                    # on this word were found using Steiner patterns?
                    if any('steiner_0' in pattern
                           for pattern in part.connective_patterns):
                        keep_part = False
                        break
                    # TODO: add check for duplicates in other cases?
            if keep_part:
                causation_instances.append(CausationInstance(
                    sentence, connective=part.connective,
                    cause=part.cause, effect=part.effect))

        return causation_instances


class CausationPatternFilterStage(Stage):
    def __init__(self, classifier, name):
        super(CausationPatternFilterStage, self).__init__(
            name=name, model=PatternBasedCausationFilter(classifier))

    consumed_attributes = ['possible_causations']

    def _label_instance(self, document, sentence, predicted_causations):
        sentence.causation_instances = predicted_causations

    def _make_evaluator(self):
        return IAAEvaluator(False, False,
                            FLAGS.causality_cc_print_test_instances, True, True)
