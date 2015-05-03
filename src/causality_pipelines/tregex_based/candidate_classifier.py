from gflags import DEFINE_list, DEFINE_integer, DEFINE_bool, FLAGS, DuplicateFlagError

import logging

from causality_pipelines.tregex_based import PairwiseCausalityStage
from data import Token, CausationInstance
from pipeline import ClassifierStage
from pipeline.models import ClassifierPart, ClassifierModel
from pipeline.feature_extractors import KnownValuesFeatureExtractor, FeatureExtractor

try:
    DEFINE_list(
        'pw_candidate_features', ['pos1', 'pos2', 'wordsbtw', 'deppath',
                                  'deplen', 'tenses', 'connective'],
        'Features to use for simple causality model')
    DEFINE_integer('pw_candidate_max_wordsbtw', 10,
                   "Pairwise classifier: maximum number of words between"
                   " phrases before just making the value the max")
    DEFINE_integer('pw_candidate_max_dep_path_len', 3,
                   "Pairwise classifier: Maximum number of dependency path steps"
                   " to allow before just making the value 'LONG-RANGE'")
    DEFINE_bool('pw_candidate_print_instances', False,
                'Pairwise classifier: Whether to print true positive, false'
                ' positive, and false negative instances after testing')
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)

class PhrasePairPart(ClassifierPart):
    def __init__(self, sentence, head_token_1, head_token_2,
                 connective_pattern, label):
        super(PhrasePairPart, self).__init__(sentence, label)
        self.head_token_1 = head_token_1
        self.head_token_2 = head_token_2
        self.connective_pattern = connective_pattern

class PhrasePairModel(ClassifierModel):
    def __init__(self, classifier):
        super(PhrasePairModel, self).__init__(
            PhrasePairPart,
            PhrasePairModel.FEATURE_EXTRACTORS,
            FLAGS.pw_candidate_features, classifier)

    @staticmethod
    def words_btw_heads(part):
        words_btw = part.instance.count_words_between(
            part.head_token_1, part.head_token_2)
        return min(words_btw, FLAGS.pw_candidate_max_wordsbtw)

    @staticmethod
    def extract_dep_path(part):
        source = part.head_token_1
        target = part.head_token_2
        # Arbitrary convention to ensure that the string comes out the same
        # no matter which direction the dependency path goes: earlier start
        # offset is source.
        if source.start_offset > target.start_offset:
            source, target = target, source
        deps = part.instance.extract_dependency_path(source, target, False)
        if len(deps) > FLAGS.pw_candidate_max_dep_path_len:
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
        if head.parent_sentence is PhrasePairModel.__cached_tenses_sentence:
            try:
                return PhrasePairModel.__cached_tenses[head]
            except KeyError:
                pass
        else:
            PhrasePairModel.__cached_tenses_sentence = head.parent_sentence
            PhrasePairModel.__cached_tenses = {}

        tense = head.parent_sentence.get_auxiliaries_string(head)
        PhrasePairModel.__cached_tenses[head] = tense
        return tense


    # We can't initialize this properly yet because we don't have access to the
    # class' static methods to define the list.
    FEATURE_EXTRACTORS = []

PhrasePairModel.FEATURE_EXTRACTORS = [
    KnownValuesFeatureExtractor('pos1', lambda part: part.head_token_1.pos,
                                Token.ALL_POS_TAGS),
    KnownValuesFeatureExtractor('pos2', lambda part: part.head_token_2.pos,
                                Token.ALL_POS_TAGS),
    # Generalized POS tags don't seem to be that useful.
    KnownValuesFeatureExtractor(
        'pos1gen', lambda part: part.head_token_1.get_gen_pos(),
        Token.ALL_POS_TAGS),
    KnownValuesFeatureExtractor(
        'pos2gen', lambda part: part.head_token_2.get_gen_pos(),
        Token.ALL_POS_TAGS),
    FeatureExtractor('wordsbtw', PhrasePairModel.words_btw_heads,
                     FeatureExtractor.FeatureTypes.Numerical),
    FeatureExtractor('deppath', PhrasePairModel.extract_dep_path),
    FeatureExtractor('deplen',
                     lambda part: len(part.instance.extract_dependency_path(
                        part.head_token_1, part.head_token_2)),
                     FeatureExtractor.FeatureTypes.Numerical),
    # TODO: This assumes that we will not have to worry about multiple patterns
    # matching simultaneously. Should we make that assumption?
    FeatureExtractor('connective', lambda part: part.connective_pattern),
    FeatureExtractor('tenses',
                     lambda part: '/'.join(
                        [PhrasePairModel.extract_tense(head)
                         for head in part.head_token_1, part.head_token_2]))
]


class PairwiseCandidateClassifierStage(ClassifierStage, PairwiseCausalityStage):
    def __init__(self, classifier, name):
        super(PairwiseCandidateClassifierStage, self).__init__(
            name=name, models=[PhrasePairModel(classifier)],
            print_test_instances=FLAGS.pw_candidate_print_instances)

    CONSUMED_ATTRIBUTES = ['possible_causations']

    def _extract_parts(self, sentence, is_train):
        parts = [PhrasePairPart(sentence, pc.arg1, pc.arg2,
                 pc.matching_pattern, pc.correct)
                 for pc in sentence.possible_causations]
        return parts

    def _decode_labeled_parts(self, sentence, labeled_parts):
        sentence.causation_instances = []
        for part in [p for p in labeled_parts if p.label]:
            causation = CausationInstance(sentence)
            # The only part type is phrase pair, so we don't have to worry
            # about checking the part type.
            # We know it's a pair of phrases related by causation, so one is
            # the cause and one is the effect, but we don't actually know
            # which is which. We arbitrarily choose to call the one with the
            # earlier head the cause. We leave the connective unset.
            cause, effect = self.normalize_order(
                (part.head_token_1, part.head_token_2))
            # Causations assume their arguments are lists of tokens.
            causation.cause, causation.effect = [cause], [effect]
            sentence.causation_instances.append(causation)

    def _begin_evaluation(self):
        ''' Select correct ancestor for this method '''
        return PairwiseCausalityStage._begin_evaluation(self)

    def _complete_evaluation(self):
        ''' Select correct ancestor for this method '''
        return PairwiseCausalityStage._complete_evaluation(self)

    def _evaluate(self, sentences, original_sentences):
        expected_causations = [sentence.causation_instances
                                     for sentence in original_sentences]
        for sentence, expected_causation_set in zip(sentences,
                                                    expected_causations):
            predicted_cause_effect_pairs = [i.get_cause_and_effect_heads() for
                                            i in sentence.causation_instances]
            expected_cause_effect_pairs = [i.get_cause_and_effect_heads() for
                                           i in expected_causation_set]
            self.match_causation_pairs(
                expected_cause_effect_pairs, predicted_cause_effect_pairs,
                self._tp_pairs, self._fp_pairs, self._fn_pairs,
                self._all_instances_metrics)
