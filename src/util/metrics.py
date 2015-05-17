from __future__ import absolute_import
import copy
from gflags import DEFINE_bool, FLAGS, DuplicateFlagError
import logging
import numpy as np
from nltk.metrics import confusionmatrix
from util.scipy import add_rows_and_cols_to_matrix

try:
    DEFINE_bool('metrics_log_raw_counts', False,
                "Log raw counts (TP, FP, etc.) for evaluation or IAA metrics.");
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)

safe_divisor = lambda divisor: divisor if divisor != 0 else np.nan

def f1(precision, recall):
    return 2 * precision * recall / safe_divisor(precision + recall)

class ClassificationMetrics(object):
    def __init__(self, tp=0, fp=0, fn=0, tn=None, finalize=True):
        # Often there is no well-defined concept of a true negative, so it
        # defaults to undefined.
        self._tp = tp
        self._fp = fp
        self._tn = tn
        self._fn = fn

        if finalize:
            self._finalize_counts()
        else:
            self._finalized = False

        #assert tp >= 0 and fp >= 0 and fn >= 0 and (tn is None or tn >= 0), (
        #    'Invalid raw metrics values (%s)' % ((tp, fp, fn, tn),))

    def __add__(self, other):
        summed = copy.copy(self)
        summed._tp += other._tp
        summed._fp += other._fp
        summed._tn += other._tn
        if summed._fn is None or other._fn is None:
            summed._fn = None
        else:
            summed._fn += other._fn
        summed._finalized = False
        return summed

    def _finalize_counts(self):
        tp = float(self._tp)
        self._precision = tp / safe_divisor(tp + self._fp)
        self._recall = tp / safe_divisor(tp + self._fn)
        self._f1 = f1(self._precision, self._recall)
        if self._tn is not None:
            self._accuracy = (tp + self._tn) / safe_divisor(
                tp + self._tn + self._fp + self._fn)
        else:
            self._accuracy = float('nan')
            self._tn = self._accuracy
        self._finalized = True

    def __str__(self):
        if not self._finalized:
            self._finalize_counts()

        if FLAGS.metrics_log_raw_counts:
            return ('TP: %g\n'
                    'TN: %g\n'
                    'FP: %g\n'
                    'FN: %g\n'
                    'Accuracy: %g\n'
                    'Precision: %g\n'
                    'Recall: %g\n'
                    'F1: %g') % (
                        self._tp, self._tn, self._fp, self._fn, self._accuracy,
                        self._precision, self._recall, self._f1)
        else:
            return ('Accuracy: %g\n'
                    'Precision: %g\n'
                    'Recall: %g\n'
                    'F1: %g') % (
                        self._accuracy, self._precision, self._recall,
                        self._f1)

    @staticmethod
    def average(metrics_list, ignore_nans=True):
        '''
        Averaging produces a technically non-sensical ClassificationMetrics
        object: the usual relationships do not hold between the properties.
        To get around this, we manually modify the underlying attributes, then
        reassure the object that it's been finalized.
        '''
        avg = ClassificationMetrics(0, 0, 0, None, False)
        property_names = (ClassificationMetrics.MUTABLE_PROPERTY_NAMES +
                          ClassificationMetrics.DERIVED_PROPERTY_NAMES)
        for property_name in property_names:
            underlying_property_name = '_' + property_name
            values = [getattr(m, underlying_property_name)
                      for m in metrics_list]
            if ignore_nans:
                values = [v for v in values if not np.isnan(v)]
            setattr(avg, underlying_property_name,
                    sum(values) / safe_divisor(float(len(values))))
        avg._finalized = True
        return avg

    ''' We need a bunch of extra functions to support property creation. '''

    @staticmethod
    def _make_mutable_getter(property_name):
        def getter(self):
            if not self._finalized:
                self._finalize_counts()
            return getattr(self, '_' + property_name)
        return getter

    @staticmethod
    def _make_derived_getter(property_name):
        return lambda self: getattr(self, '_' + property_name)

    @staticmethod
    def _make_real_setter(property_name):
        def setter(self, value):
            setattr(self, '_' + property_name, value)
            self._finalized = False
        return setter

    @staticmethod
    def _make_derived_setter(property_name):
        def setter(self, value):
            raise ValueError('%s property is not directly modifiable'
                             % property_name)
        return setter

    MUTABLE_PROPERTY_NAMES = ['tp', 'fp', 'fn', 'tn']
    DERIVED_PROPERTY_NAMES = ['accuracy', 'precision', 'recall', 'f1']

for property_name in ClassificationMetrics.MUTABLE_PROPERTY_NAMES:
    getter = ClassificationMetrics._make_derived_getter(property_name)
    setter = ClassificationMetrics._make_real_setter(property_name)
    setattr(ClassificationMetrics, property_name, property(getter, setter))
for property_name in ClassificationMetrics.DERIVED_PROPERTY_NAMES:
    getter = ClassificationMetrics._make_mutable_getter(property_name)
    setter = ClassificationMetrics._make_derived_setter(property_name)
    setattr(ClassificationMetrics, property_name, property(getter, setter))


def diff_binary_vectors(predicted, gold):
    # Make sure np.where works properly
    predicted = np.array(predicted)
    gold = np.array(gold)

    tp = np.count_nonzero((predicted == 1) & (gold == 1))
    tn = np.count_nonzero((predicted == 0) & (gold == 0))
    fp = np.count_nonzero((predicted == 1) & (gold == 0))
    fn = np.count_nonzero((predicted == 0) & (gold == 1))
    return ClassificationMetrics(tp, fp, fn, tn)


class ConfusionMatrix(confusionmatrix.ConfusionMatrix):
    def __init__(self, *args, **kwargs):
        kwargs['sort_by_count'] = False
        super(ConfusionMatrix, self).__init__(*args, **kwargs)
        self._confusion = np.array(self._confusion)
        self.class_names = self._values
        
    def __add__(self, other):
        # Deal with the possibility of an empty matrix.
        if self._confusion.shape[0] == 0:
            return copy.deepcopy(other)
        elif other._confusion.shape[0] == 0:
            return copy.deepcopy(self)

        # First, create the merged labels list, and figure out what columns
        # we'll need to insert in the respective matrices.
        # Because we've disabled sort by count, _values is already sorted in
        # alphabetical order. 
        i = 0
        j = 0
        self_cols_to_add = [0 for _ in range(len(self._values) + 1)]
        other_cols_to_add = [0 for _ in range(len(other._values) + 1)]
        merged_values = []
        while i < len(self._values) and j < len(other._values):
            if self._values[i] < other._values[j]:
                # I have an item other doesn't. Record where to insert it.
                merged_values.append(self._values[i])
                other_cols_to_add[j] += 1
                i += 1
            elif self._values[i] > other._values[j]:
                # Other has an item I don't. Record where to insert it.
                merged_values.append(other._values[j])
                self_cols_to_add[i] += 1
                j += 1
            else:
                merged_values.append(self._values[i])
                i += 1
                j += 1
        if i < len(self._values): # still some self values left
            merged_values.extend(self._values[i:])
            other_cols_to_add[-1] = len(self._values) - i
        if j < len(other._values): # still some other values left
            merged_values.extend(other._values[j:])
            self_cols_to_add[-1] = len(other._values) - j

        augmented_self_matrix = add_rows_and_cols_to_matrix(self._confusion,
                                                            self_cols_to_add)
        augmented_other_matrix = add_rows_and_cols_to_matrix(other._confusion,
                                                             other_cols_to_add)

        new_matrix = copy.copy(self)
        new_matrix._values = merged_values
        new_matrix.class_names = merged_values
        new_matrix._indices = {val: i for i, val in enumerate(merged_values)}
        new_matrix._confusion = augmented_self_matrix + augmented_other_matrix
        new_matrix._max_conf = max(self._max_conf, other._max_conf)
        new_matrix._total = self._total + other._total
        new_matrix._correct = self._correct + other._correct

        return new_matrix
    
    def __radd__(self, other):
        return other.__add__(self)

    def pretty_format_metrics(self):
        return ('% Agreement: {:.2}\nKappa: {:.2}\n'
                'Micro F1: {:.2}\nMacro F1: {:.2}'.format(
                    self.pct_agreement(), self.kappa(), self.f1_micro(),
                    self.f1_macro()))

    def pretty_format(self, *args, **kwargs):
        """
        Accepts a 'metrics' keyword argument (or fifth positional argument)
        indicating whether to print the agreement metrics, as well.
        """
        try:
            log_metrics = kwargs.pop('metrics')
        except KeyError:
            log_metrics = False
        if self._values:
            pp = super(ConfusionMatrix, self).pretty_format(*args, **kwargs)
            if (len(args) > 4 and args[4] == True) or log_metrics:
                pp += self.pretty_format_metrics()
        else:
            pp = repr(self) # <ConfusionMatrix: 0/0 correct>
        return pp

    def num_agreements(self):
        return self._correct

    def pct_agreement(self):
        return self._correct / safe_divisor(float(self._total))

    def kappa(self):
        if not self._total:
            return float('nan')

        row_totals = np.sum(self._confusion, axis=1)
        col_totals = np.sum(self._confusion, axis=0)
        total_float = safe_divisor(float(self._total))
        agree_by_chance = sum([(row_total * col_total) / total_float
                               for row_total, col_total
                               in zip(row_totals, col_totals)])
        kappa = (self._correct - agree_by_chance) / safe_divisor(
            self._total - agree_by_chance)
        return kappa

    def _get_f1_stats_arrays(self):
        # Which axis we call gold and which we call test is pretty arbitrary.
        # It doesn't matter, because F1 is symmetric.
        tp = self._confusion.diagonal()
        fp = self._confusion.sum(0) - tp
        fn = self._confusion.sum(1) - tp
        return (tp, fp, fn)

    def f1_micro(self):
        _, fp, fn = self._get_f1_stats_arrays()
        p_micro = self._correct / float(safe_divisor(self._correct + fp.sum()))
        r_micro = self._correct / float(safe_divisor(self._correct + fn.sum()))
        return f1(p_micro, r_micro)

    def f1_macro(self):
        tp, fp, fn = self._get_f1_stats_arrays()
        p_macro_fractions = tp / np.sum([tp, fp], axis=0, dtype=float)
        p_macro = np.average(p_macro_fractions)
        r_macro_fractions = tp / np.sum([tp, fn], axis=0, dtype=float)
        r_macro = np.average(r_macro_fractions)
        return f1(p_macro, r_macro)


class AccuracyMetrics(object):
    def __init__(self, correct, incorrect):
        self.correct = correct
        self.incorrect = incorrect
        self.accuracy = correct / safe_divisor(float(correct + incorrect))

    def pretty_format(self):
        if FLAGS.metrics_log_raw_counts:
            return ('Correct: {:}\nIncorrect: {:}\n% Agreement: {:.2}'
                    .format(self.correct, self.incorrect, self.accuracy))
        else:
            return '% Agreement: {:.2}'.format(self.accuracy)

    def __str__(self):
        return self.pretty_format()

    @staticmethod
    def average(metrics):
        assert metrics, "Cannot average empty metrics list"
        new_metrics = AccuracyMetrics(np.nan, np.nan)
        new_metrics.correct = np.mean([m.correct for m in metrics])
        new_metrics.incorrect = np.mean([m.incorrect for m in metrics])
        new_metrics.accuracy = np.mean([m.accuracy for m in metrics])
