#!/usr/bin/env python

from gflags import (FLAGS, DEFINE_enum, DEFINE_bool, DEFINE_integer,
                    DEFINE_float, DEFINE_string, DuplicateFlagError, FlagsError)
import logging
import numpy as np
import os
from sklearn import tree, neighbors, linear_model, svm, ensemble, naive_bayes
import subprocess
import sys

from causality_pipelines import (remove_smaller_matches, StanfordNERStage,
                                 IAAEvaluator)
from causality_pipelines.baseline import BaselineStage
from causality_pipelines.baseline.combiner import BaselineCombinerStage
from causality_pipelines.baseline.most_freq_filter import (
    MostFreqSenseFilterStage)
from causality_pipelines.candidate_filter import (CausationPatternFilterStage,
                                                  CausalPatternClassifierModel)
from causality_pipelines.regex_based.crf_stage import ArgumentLabelerStage
from causality_pipelines.regex_based.regex_stage import RegexConnectiveStage
from causality_pipelines.tregex_based.arg_span_stage import ArgSpanStage
from causality_pipelines.tregex_based.tregex_stage import TRegexConnectiveStage
from data.io import DirectoryReader, CausalityStandoffReader
from pipeline import Pipeline, SimpleStage
from pipeline.models import ClassBalancingClassifierWrapper
from util import print_indented


# TODO: factor out a generic driver function?

try:
    DEFINE_enum('classifier_model', 'logistic',
                ['tree', 'knn', 'logistic', 'svm', 'forest', 'nb'],
                'What type of machine learning model to use as the underlying'
                ' causality filter classifier')
    DEFINE_float(
        'rebalance_ratio', 1.0,
        'The maximum ratio by which to rebalance classes for training')
    DEFINE_bool('eval_with_cv', False,
               'Evaluate with cross-validation. Overrides --evaluate flag, and'
               ' causes both train and test to be combined.')
    DEFINE_bool('debug', False,
                'Whether to print debug-level logging.')
    DEFINE_integer('seed', None, 'Seed for the numpy RNG.')
    DEFINE_enum('pipeline_type', 'tregex',
                ['tregex', 'regex', 'baseline', 'tregex+baseline',
                 'regex+baseline', 'tregex_mostfreq', 'regex_mostfreq'],
                'Which causality pipeline to run')
    DEFINE_bool('filter_overlapping', True,
                'Whether to filter smaller connectives that overlap with larger'
                ' ones')
    DEFINE_bool('save_models', False,
                "Whether to save pipeline models post-train (if not doing CV).")
    DEFINE_string('models_dir', None,
                  "Directory in which to save models and from which to load"
                  " them. Relative to the working directory. Defaults to"
                  " ../models/<pipeline type>.")
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)


def get_stages(candidate_classifier):
    BASELINE_CAUSATIONS_NAME = 'baseline_causations'

    if FLAGS.pipeline_type == 'tregex':
        stages = [TRegexConnectiveStage('TRegex connectives'),
                  StanfordNERStage('NER'),
                  ArgSpanStage('Argument span expander'),
                  CausationPatternFilterStage(candidate_classifier,
                                              'Candidate classifier')]
    elif FLAGS.pipeline_type == 'regex':
        stages = [RegexConnectiveStage('Regex connectives'),
                  StanfordNERStage('NER'),
                  ArgumentLabelerStage('CRF arg labeler'),
                  CausationPatternFilterStage(candidate_classifier,
                                              'Candidate classifier')]
    elif FLAGS.pipeline_type == 'tregex+baseline':
        stages = [BaselineStage('Baseline', BASELINE_CAUSATIONS_NAME),
                  TRegexConnectiveStage('TRegex connectives'),
                  StanfordNERStage('NER'),
                  ArgSpanStage('Argument span expander'),
                  CausationPatternFilterStage(candidate_classifier,
                                              'Candidate classifier'),
                  BaselineCombinerStage('Combiner', BASELINE_CAUSATIONS_NAME)]
    elif FLAGS.pipeline_type == 'regex+baseline':
        stages = [BaselineStage('Baseline', BASELINE_CAUSATIONS_NAME),
                  RegexConnectiveStage('Regex connectives'),
                  StanfordNERStage('NER'),
                  ArgumentLabelerStage('CRF arg labeler'),
                  CausationPatternFilterStage(candidate_classifier,
                                              'Candidate classifier'),
                  BaselineCombinerStage('Combiner', BASELINE_CAUSATIONS_NAME)]
    elif FLAGS.pipeline_type == 'baseline':
        stages = [BaselineStage('Baseline')]
    elif FLAGS.pipeline_type == 'tregex_mostfreq':
        stages = [TRegexConnectiveStage('TRegex connectives'),
                  ArgSpanStage('Argument span expander'),
                  MostFreqSenseFilterStage('Most frequent sense filter')]
    elif FLAGS.pipeline_type == 'regex_mostfreq':
        stages = [RegexConnectiveStage('Regex connectives'),
                  StanfordNERStage('NER'),
                  ArgumentLabelerStage('CRF arg labeler'),
                  MostFreqSenseFilterStage('Most frequent sense filter')]

    if FLAGS.filter_overlapping and FLAGS.pipeline_type != 'baseline':
        smaller_filter_stage = SimpleStage(
            'Filter smaller connectives', remove_smaller_matches,
            lambda: IAAEvaluator(False, False,
                                 FLAGS.filter_print_test_instances, True, True))
        stages.append(smaller_filter_stage)
    return stages


# def main(argv):
if __name__ == '__main__':
    argv = sys.argv

    try:
        FLAGS.Reset()
        FLAGS(argv)  # parse flags
        if not FLAGS.models_dir:
            FLAGS.models_dir = os.path.join('..', 'models', FLAGS.pipeline_type)

        # Print command line in case we ever want to re-run from output file
        print "Flags:"
        print_indented(1, FLAGS.FlagsIntoString())
        print "Git info:"
        print_indented(1, subprocess.check_output("git rev-parse HEAD".split()),
                       "Modified:", sep='')
        print_indented(2, subprocess.check_output("git ls-files -m".split()))
        print "Available filter features:"
        print_indented(
            1, '\n'.join(sorted(e.name for e in
                CausalPatternClassifierModel.all_feature_extractors)))
    except FlagsError, e:
        print '%s\nUsage: %s ARGS\n%s' % (e, sys.argv[0], FLAGS)
        sys.exit(1)

    logging.basicConfig(
        format='%(filename)s:%(lineno)s:%(levelname)s: %(message)s',
        level=[logging.INFO, logging.DEBUG][FLAGS.debug])
    logging.captureWarnings(True)

    seed = FLAGS.seed
    if seed is None:
        seed = int(os.urandom(4).encode('hex'), 16)
    np.random.seed(seed)
    print "Using seed:", seed

    if FLAGS.classifier_model == 'tree':
        candidate_classifier = tree.DecisionTreeClassifier()
    elif FLAGS.classifier_model == 'knn':
        candidate_classifier = neighbors.KNeighborsClassifier()
    elif FLAGS.classifier_model == 'logistic':
        candidate_classifier = linear_model.LogisticRegression(
            penalty='l1', class_weight='balanced', tol=1e-5)
    elif FLAGS.classifier_model == 'svm':
        candidate_classifier = svm.SVC()
    elif FLAGS.classifier_model == 'forest':
        candidate_classifier = ensemble.RandomForestClassifier(n_jobs=-1)
    elif FLAGS.classifier_model == 'nb':
        candidate_classifier = naive_bayes.MultinomialNB()

    if FLAGS.classifier_model != 'logistic':
        candidate_classifier = ClassBalancingClassifierWrapper(
            candidate_classifier, FLAGS.rebalance_ratio)

    stages = get_stages(candidate_classifier)

    causality_pipeline = Pipeline(
        stages, DirectoryReader((CausalityStandoffReader.FILE_PATTERN,),
                                CausalityStandoffReader())) # ,
        # copy_fn=StanfordParsedSentence.shallow_copy_doc_with_sentences_fixed)

    if FLAGS.eval_with_cv:
        eval_results = causality_pipeline.cross_validate()
        causality_pipeline.print_eval_results(eval_results)
    else:
        if FLAGS.train_paths:
            causality_pipeline.train()
            if FLAGS.save_models:
                causality_pipeline.save_models(FLAGS.models_dir)
        else: # We're not training; load models
            causality_pipeline.load_models(FLAGS.models_dir)

        if FLAGS.evaluate:
            eval_results = causality_pipeline.evaluate()
            causality_pipeline.print_eval_results(eval_results)
        elif FLAGS.test_paths:
            causality_pipeline.test()

#if __name__ == '__main__':
#    main(sys.argv)
