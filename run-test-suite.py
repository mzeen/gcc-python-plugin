#   Copyright 2011, 2012 David Malcolm <dmalcolm@redhat.com>
#   Copyright 2011, 2012 Red Hat, Inc.
#
#   This is free software: you can redistribute it and/or modify it
#   under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see
#   <http://www.gnu.org/licenses/>.

# Test cases are in the form of subdirectories of the "tests" directory; any
# subdirectory containing a "script.py" is regarded as a test case.
#
# A test consists of:
#   input*.c/cc: C/C++ source code to be compiled
#   script.py:  a Python script to be run by GCC during said compilation
#   stdout.txt: (optional) the expected stdout from GCC (empty if not present)
#   stderr.txt: (optional) as per stdout.txt
#   getopts.py: (optional) if present, stdout from this script is
#               added to GCC's invocation options
#   metadata.ini: (optional) if present, can override other properties of the
#                 test (see below)
#
# This runner either invokes all tests, or just a subset, if supplied the
# names of the subdirectories as arguments.  All test cases within the given
# directories will be run.

# The optional metadata.ini can contain these sections:
#
# [WhenToRun]
#   required_features = whitespace-separated list of #defines that must be on
#                       within autogenerated-config.h
#
# [ExpectedBehavior]
#   exitcode = integer value, for overriding defaults
#

import glob
import os
import multiprocessing
import re
import sys
from distutils.sysconfig import get_python_inc
from subprocess import Popen, PIPE

import six
from six.moves import configparser

from cpybuilder import CommandError

from testcpychecker import get_gcc_version

PLUGIN_NAME = os.environ.get('PLUGIN_NAME', 'python')

class CompilationError(CommandError):
    def __init__(self, out, err, p, args):
        CommandError.__init__(self, out, err, p)
        self.args = args
        
    def _describe_activity(self):
        return 'compiling: %s' % ' '.join(self.args)

class TestStream:
    def __init__(self, exppath):
        self.exppath = exppath
        if os.path.exists(exppath):
            with open(exppath) as f:
                expdata = f.read()
            # The expected data is for Python 2
            # Apply python3 fixups as necessary:
            if six.PY3:
                expdata = expdata.replace('<type ', '<class ')
                expdata = expdata.replace('__builtin__', 'builtins')
                # replace long literals with int literals:
                expdata = re.sub('([0-9]+)L', '\g<1>', expdata)
                expdata = re.sub('(0x[0-9a-f]+)L', '\g<1>', expdata)
                expdata = expdata.replace('PyStringObject',
                                          'PyBytesObject')
                expdata = expdata.replace('PyString_Type',
                                          'PyBytes_Type')
            # The expected data is for 64-bit builds of Python
            # Fix it up for 32-bit builds as necessary:
            if six.MAXSIZE == 0x7fffffff:
                expdata = expdata.replace('"Py_ssize_t *" (pointing to 64 bits)',
                                          '"Py_ssize_t *" (pointing to 32 bits)')
                expdata = expdata.replace('0x8000000000000000', '0x80000000')
                expdata = expdata.replace('0x7fffffffffffffff', '0x7fffffff')
            self.expdata = expdata
        else:
            self.expdata = ''

    def _cleanup(self, text):
        result = ''

        # Debug builds of Python add reference-count logging lines of
        # this form:
        #   "[84507 refs]"
        # Strip such lines out:
        text = re.sub(r'(\[[0-9]+ refs\]\n)', '', text)
        for line in text.splitlines():
            if line.startswith("Preprocessed source stored into"):
                # Handle stuff like this that changes every time:
                # "Preprocessed source stored into /tmp/ccRm9Xgx.out file, please attach this to your bugreport."
                continue
            # Remove exact pointer addresses from repr():
            line = re.sub(' object at (0x[0-9a-f]*)>',
                          ' object at 0xdeadbeef>',
                          line)

            # Remove exact numbers from declarations
            # (e.g. from "D.12021->fieldA" to "D.nnnnn->fieldA"):
            line = re.sub('D.([0-9]+)', 'D.nnnnn', line)
            line = re.sub('VarDecl\(([0-9]+)\)', 'VarDecl(nnnn)', line)
            line = re.sub('ParmDecl\(([0-9]+)\)', 'ParmDecl(nnnn)', line)
            line = re.sub('LabelDecl\(([0-9]+)\)', 'LabelDecl(nnnn)', line)

            # Remove exact numbers from types
            # (e.g. from "int (*<T513>) (int)" to "int (*<Tnnn>) (int)"):
            line = re.sub('<T([0-9a-f]+)>', '<Tnnn>', line)

            # Remove exact path to Python header file and line number
            # e.g.
            #   unknown struct PyObject * from /usr/include/python2.7/pyerrors.h:135
            #   unknown struct PyObject * from /usr/include/python3.2mu/pyerrors.h:132
            # should both become:
            #   unknown struct PyObject * from /usr/include/python?.?/pyerrors.h:nn
            line = re.sub('/usr/include/python(.*)/(.*).h:[0-9]+',
                          r'/usr/include/python?.?/\2.h:nn',
                          line)

            # Convert to the Python 3 format for the repr() of a frozenset:
            # e.g. from:
            #   frozenset([0, 1, 2])
            # to:
            #   frozenset({0, 1, 2})
            # and from:
            #   frozenset([])
            # to:
            #   frozenset()
            line = re.sub(r'frozenset\(\[\]\)', 'frozenset()', line)
            line = re.sub(r'frozenset\(\[(.*)\]\)',
                          r'frozenset({\1})',
                          line)

            # Avoid further 32-bit vs 64-bit differences due to int vs long
            # overflow:
            line = re.sub('0x7fffffffL', '0x7fffffff', line)
            line = re.sub('0x7ffffffeL', '0x7ffffffe', line)
            line = re.sub('0xffffffffL', '0xffffffff', line)
            line = re.sub('0xfffffffeL', '0xfffffffe', line)

            # GCC 4.7 tracks macro expansions, and this can change the column
            # numbers in error reports:
            line = re.sub(r'input.c:([0-9]+):([0-9]+):',
                          r'input.c:\1:nn:',
                          line)

            # GCC 4.8's output sometimes omits the filename prefix for a
            # diagnostic:
            m = re.match(r"(.+): (In function '.+':)", line)
            if m:
                line = m.group(2)

            # For some reason, some of the test cases emit (long int)
            # refcounts, rather than (Py_ssize_t)
            # I think this is to do with borrowed refs
            line = re.sub(r'r->ob_refcnt: \(long int\)val',
                          r'r->ob_refcnt: (Py_ssize_t)val',
                          line)

            # Python 3.3's unicode reimplementation drops the macro redirection
            # to narrow/wide implementations ("UCS2"/"UCS4")
            line = re.sub('PyUnicodeUCS4_AsUTF8String', 'PyUnicode_AsUTF8String', line)

            # Avoid hardcoding timings from unittest's output:
            line = re.sub(r'Ran ([0-9]+ tests?) in ([0-9]+\.[0-9]+s)',
                          r'Ran \1 in #s',
                          line)

            result += line + '\n'

        return result

    def check_for_diff(self, out, err, p, args, label, writeback):
        actual = self._cleanup(self.actual)
        expdata = self._cleanup(self.expdata)
        if writeback:
            # Special-case mode: don't compare, instead refresh the "gold"
            # output by writing back to disk:
            if self.expdata != '':
                with open(self.exppath, 'w') as f:
                    f.write(actual)
            return
        if actual != expdata:
            raise UnexpectedOutput(out, err, p, args, self, label)

    def diff(self, label):
        from difflib import unified_diff
        result = ''
        for line in unified_diff(self._cleanup(self.expdata).splitlines(),
                                 self._cleanup(self.actual).splitlines(),
                                 fromfile='Expected %s (after cleaning)' % label,
                                 tofile='Actual %s (after cleaning)' % label,
                                 lineterm=""):
            result += '%s\n' % line
        return result

class UnexpectedOutput(CompilationError):
    def __init__(self, out, err, p, args, stream, label):
        CompilationError.__init__(self, out, err, p, args)
        self.stream = stream
        self.label = label
    
    def _extra_info(self):
        return self.stream.diff(self.label)


def get_source_files(testdir):
    """
    Locate source files within the test directory,
    of the form "input*.c", "input*.cc" etc
    trying various different suffixes by programming language
    """
    inputfiles = []
    suffixes = ['.c', '.cc', '.java', '.f', '.f90']
    for suffix in suffixes:
        from glob import glob
        inputfiles += glob(os.path.join(testdir, 'input*%s' % suffix))
    if not inputfiles:
        raise RuntimeError('Source file not found')
    return inputfiles

config_h = 'autogenerated-config.h'
def parse_autogenerated_config_h():
    from collections import OrderedDict
    result = OrderedDict()
    with open(config_h) as f:
        for line in f.readlines():
            m = re.match('#define (.+)', line)
            if m:
                result[m.group(1)] = True
            m = re.match('#undef (.+)', line)
            if m:
                result[m.group(1)] = False
    return result

features = parse_autogenerated_config_h()

CC = os.environ.get('CC', 'gcc')
GCC_VERSION = get_gcc_version()

class SkipTest(Exception):
    def __init__(self, reason):
        self.reason = reason

def run_test(testdir):
    # Compile each 'input.c', using 'script.py'
    # Assume success and empty stdout; compare against expected stderr, or empty if file not present
    inputfiles = get_source_files(testdir)
    outfile = os.path.join(testdir, 'output.o')
    script_py = os.path.join(testdir, 'script.py')
    out = TestStream(os.path.join(testdir, 'stdout.txt'))
    err = TestStream(os.path.join(testdir, 'stderr.txt'))

    cp = configparser.SafeConfigParser()
    metadatapath = os.path.join(testdir, 'metadata.ini')
    cp.read([metadatapath])

    if cp.has_section('WhenToRun'):
        if cp.has_option('WhenToRun', 'required_features'):
            required_features = cp.get('WhenToRun', 'required_features').split()
            for feature in required_features:
                if feature not in features:
                    raise ValueError('%s in %s not found in %s'
                                     % (feature, metadatapath, config_h))
                if not features[feature]:
                    raise SkipTest('required feature %s not available in %s'
                                   % (feature, config_h))

    env = dict(os.environ)
    env['LC_ALL'] = 'C'

    # Generate the command-line for invoking gcc:
    args = [CC]
    if len(inputfiles) == 1:
        args += ['-c'] # (don't run the linker)
    else:
        args += ['-fPIC', '-shared']
        # Force LTO when there's more than one source file:
        args += ['-flto', '-flto-partition=none']

    if GCC_VERSION >= 4008:
        # GCC 4.8 started showing the source line where the problem is,
        # followed by another line showing a caret indicating column.
        # This is a great usability feature, but totally breaks our "gold"
        # output, so turn it off for running tests:
        args += ['-fno-diagnostics-show-caret']

        # Similarly, the macro expansion tracking is great for usability,
        # but breaks the "gold" output, so we disable it during tests:
        args += ['-ftrack-macro-expansion=0']

    args += ['-o', outfile]
    args += ['-fplugin=%s' % os.path.abspath('%s.so' % PLUGIN_NAME),
             '-fplugin-arg-%s-script=%s' % (PLUGIN_NAME, script_py)]

    # Force the signedness of char so that the tests have consistent
    # behavior across all archs:
    args += ['-fsigned-char']

    # Special-case: add the python include dir (for this runtime) if the C code
    # uses Python.h:
    def uses_python_headers():
        for inputfile in inputfiles:
            with open(inputfile, 'r') as f:
                code = f.read()
            if '#include <Python.h>' in code:
                return True

    if uses_python_headers():
        args += ['-I' + get_python_inc()]

    # If there's a getopts.py, run it to get additional test-specific
    # command-line options:
    getopts_py = os.path.join(testdir, 'getopts.py')
    if os.path.exists(getopts_py):
        p = Popen([sys.executable, getopts_py], stdout=PIPE, stderr=PIPE)
        opts_out, opts_err = p.communicate()
        if six.PY3:
            opts_out = opts_out.decode()
            opts_err = opts_err.decode()
        c = p.wait()
        if c != 0:
            raise CommandError()
        args += opts_out.split()

    # and the source files go at the end:
    args += inputfiles

    if options.show:
        # Show the gcc invocation:
        print(' '.join(args))

    # Invoke the compiler:
    p = Popen(args, env=env, stdout=PIPE, stderr=PIPE)
    out.actual, err.actual = p.communicate()
    if six.PY3:
        out.actual = out.actual.decode()
        err.actual = err.actual.decode()
    #print 'out: %r' % out.actual
    #print 'err: %r' % err.actual
    exitcode_actual = p.wait()

    if options.show:
        # then the user wants to see the gcc invocation directly
        sys.stdout.write(out.actual)
        sys.stderr.write(err.actual)

    # Expected exit code
    # By default, we expect success if the expected stderr is empty, and
    # and failure if it's non-empty.
    # This can be overridden if the test has a metadata.ini, by setting
    # exitcode within the [ExpectedBehavior] section:
    if err.expdata == '':
        exitcode_expected = 0
    else:
        exitcode_expected = 1
    if cp.has_section('ExpectedBehavior'):
        if cp.has_option('ExpectedBehavior', 'exitcode'):
            exitcode_expected = cp.getint('ExpectedBehavior', 'exitcode')

    # Check exit code:
    if exitcode_actual != exitcode_expected:
        sys.stderr.write(out.diff('stdout'))
        sys.stderr.write(err.diff('stderr'))
        raise CompilationError(out.actual, err.actual, p, args)

    if exitcode_expected == 0:
        assert os.path.exists(outfile)
    
    out.check_for_diff(out.actual, err.actual, p, args, 'stdout', 0)
    err.check_for_diff(out.actual, err.actual, p, args, 'stderr', 0)


from optparse import OptionParser
parser = OptionParser()
parser.add_option("-x", "--exclude",
                  action="append",
                  type="string",
                  dest="excluded_dirs",
                  help="exclude tests in DIR and below", metavar="DIR")
parser.add_option("-s", "--show",
                  action="store_true", dest="show", default=False,
                  help="Show stdout, stderr and the command line for each test")
(options, args) = parser.parse_args()

# print (options, args)

def find_tests_below(path):
    result = []
    for dirpath, dirnames, filenames in os.walk(path):
        if 'script.py' in filenames:
            result.append(dirpath)
    return result


if len(args) > 0:
    # Just run the given tests (or test subdirectories)
    testdirs = []
    for path in args:
        testdirs += find_tests_below(path)
else:
    # Run all the tests
    testdirs = find_tests_below('tests')

def exclude_test(test):
    if test in testdirs:
        testdirs.remove(test)

def exclude_tests_below(path):
    for test in find_tests_below(path):
        exclude_test(test)

# Handle exclusions:
if options.excluded_dirs:
    for path in options.excluded_dirs:
        exclude_tests_below(path)

# Certain tests don't work on 32-bit
if six.MAXSIZE == 0x7fffffff:
    # These two tests verify that we can detect int vs Py_ssize_t mismatches,
    # but on 32-bit these are the same type, so don't find anything:
    exclude_test('tests/cpychecker/PyArg_ParseTuple/with_PY_SSIZE_T_CLEAN')
    exclude_test('tests/cpychecker/PyArg_ParseTuple/without_PY_SSIZE_T_CLEAN')

    # One part of the expected output for this test assumes int vs Py_ssize_t
    # mismatch:
    exclude_test('tests/cpychecker/PyArg_ParseTuple/incorrect_converters')

    # The expected output for the following tests assumes a 64-bit build:
    exclude_test('tests/cpychecker/absinterp/casts/pointer-to-long')
    exclude_test('tests/cpychecker/absinterp/casts/pyobjectptr-to-long')
    exclude_test('tests/cpychecker/refcounts/PyArg_ParseTuple/correct_O')
    exclude_test('tests/cpychecker/refcounts/PyArg_ParseTupleAndKeywords/correct_O')
    exclude_test('tests/cpychecker/refcounts/PyInt_AsLong/correct_cast')
    exclude_test('tests/cpychecker/refcounts/PyList_Size/known-size')
    exclude_test('tests/cpychecker/refcounts/PyMapping_Size/basic')
    exclude_test('tests/cpychecker/refcounts/PyString_Size/correct')
    exclude_test('tests/cpychecker/refcounts/PyTuple_New/correct')
    exclude_test('tests/cpychecker/refcounts/module_handling')
    exclude_test('tests/cpychecker/refcounts/storage_regions/static/correct')
    exclude_test('tests/examples/cplusplus/classes')
    exclude_test('tests/plugin/constants')
    exclude_test('tests/plugin/gimple-walk-tree/dump-all')
    exclude_test('tests/plugin/gimple-walk-tree/find-one')

# Certain tests don't work for Python 3:
if six.PY3:
    # The PyInt_ API doesn't exist anymore in Python 3:
    exclude_tests_below('tests/cpychecker/refcounts/PyInt_AsLong/')
    exclude_tests_below('tests/cpychecker/refcounts/PyInt_FromLong/')

    # Similarly for the PyString_ API:
    exclude_tests_below('tests/cpychecker/refcounts/PyString_AsString')
    exclude_tests_below('tests/cpychecker/refcounts/PyString_Concat')
    exclude_tests_below('tests/cpychecker/refcounts/PyString_ConcatAndDel')
    exclude_tests_below('tests/cpychecker/refcounts/PyString_FromStringAndSize')
    exclude_tests_below('tests/cpychecker/refcounts/PyString_Size')

    # The PyCObject_ API was removed in 3.2:
    exclude_tests_below('tests/cpychecker/refcounts/PyCObject_FromVoidPtr')
    exclude_tests_below('tests/cpychecker/refcounts/PyCObject_FromVoidPtrAndDesc')

    # The following tests happen to use PyInt or PyString APIs and thus we
    # exclude them for now:
    exclude_test('tests/cpychecker/refcounts/function-that-exits') # PyString
    exclude_test('tests/cpychecker/refcounts/GIL/correct') # PyString
    exclude_test('tests/cpychecker/refcounts/handle_null_error') # PyString
    exclude_test('tests/cpychecker/refcounts/PyArg_ParseTuple/correct_O_bang') # PyString
    exclude_test('tests/cpychecker/refcounts/PyObject_CallMethodObjArgs/correct') # PyString
    exclude_test('tests/cpychecker/refcounts/PyObject_CallMethodObjArgs/incorrect') # PyString
    exclude_test('tests/cpychecker/refcounts/PyStructSequence/correct') # PyInt
    exclude_test('tests/cpychecker/refcounts/PySys_SetObject/correct') # PyString
    exclude_test('tests/cpychecker/refcounts/subclass/handling') # PyString

    # Module handling is very different in Python 2 vs 3.  For now, only run
    # this test for Python 2:
    exclude_test('tests/cpychecker/refcounts/module_handling')

    # Uses METH_OLDARGS:
    exclude_test('tests/cpychecker/refcounts/PyArg_Parse/correct_simple')

# Certain tests don't work for debug builds of Python:
if hasattr(sys, 'gettotalrefcount'):
    exclude_test('tests/cpychecker/refcounts/PyDict_SetItem/correct')
    exclude_test('tests/cpychecker/refcounts/PyDict_SetItem/incorrect')
    exclude_test('tests/cpychecker/refcounts/PyDict_SetItemString/correct')
    exclude_test('tests/cpychecker/refcounts/PyDict_SetItemString/incorrect')
    exclude_test('tests/cpychecker/refcounts/PyFloat_AsDouble/correct_PyFloatObject')
    exclude_test('tests/cpychecker/refcounts/PyList_Append/correct')
    exclude_test('tests/cpychecker/refcounts/PyList_Append/incorrect')
    exclude_test('tests/cpychecker/refcounts/PyList_Append/incorrect-loop')
    exclude_test('tests/cpychecker/refcounts/PyList_Append/null-newitem')
    exclude_test('tests/cpychecker/refcounts/PyList_Append/ticket-22')
    exclude_test('tests/cpychecker/refcounts/PyList_SET_ITEM_macro/correct')
    exclude_test('tests/cpychecker/refcounts/PyList_SET_ITEM_macro/correct_multiple')
    exclude_test('tests/cpychecker/refcounts/PyList_SET_ITEM_macro/incorrect_multiple')
    exclude_test('tests/cpychecker/refcounts/PyList_Size/known-size')
    exclude_test('tests/cpychecker/refcounts/PySequence_SetItem/correct')
    exclude_test('tests/cpychecker/refcounts/PySequence_SetItem/incorrect')
    exclude_test('tests/cpychecker/refcounts/PySequence_Size/correct')
    exclude_test('tests/cpychecker/refcounts/PyString_AsString/correct')
    exclude_test('tests/cpychecker/refcounts/PyString_AsString/incorrect')
    exclude_test('tests/cpychecker/refcounts/PySys_SetObject/correct')
    exclude_test('tests/cpychecker/refcounts/PyTuple_SET_ITEM_macro/correct')
    exclude_test('tests/cpychecker/refcounts/PyTuple_SET_ITEM_macro/correct_multiple')
    exclude_test('tests/cpychecker/refcounts/PyTuple_SET_ITEM_macro/incorrect_multiple')
    exclude_test('tests/cpychecker/refcounts/PyTuple_SetItem/correct')
    exclude_test('tests/cpychecker/refcounts/PyTuple_SetItem/correct_multiple')
    exclude_test('tests/cpychecker/refcounts/PyTuple_SetItem/incorrect_multiple')
    exclude_test('tests/cpychecker/refcounts/Py_BuildValue/correct-code-N')
    exclude_test('tests/cpychecker/refcounts/Py_BuildValue/correct-code-O')
    exclude_test('tests/cpychecker/refcounts/correct_decref')
    exclude_test('tests/cpychecker/refcounts/loop_n_times')
    exclude_test('tests/cpychecker/refcounts/loops/complex-loop-conditional-1')
    exclude_test('tests/cpychecker/refcounts/loops/complex-loop-conditional-2')
    exclude_test('tests/cpychecker/refcounts/module_handling')
    exclude_test('tests/cpychecker/refcounts/object_from_callback')
    exclude_test('tests/cpychecker/refcounts/passing_dead_object')
    exclude_test('tests/cpychecker/refcounts/returning_dead_object')
    exclude_test('tests/cpychecker/refcounts/ticket-20')
    exclude_test('tests/cpychecker/refcounts/unrecognized_function2')
    exclude_test('tests/cpychecker/refcounts/unrecognized_function4')
    exclude_test('tests/cpychecker/refcounts/use_after_dealloc')
    exclude_test('tests/examples/spelling-checker')

# This test is unreliable, due to differences in the dictionary:
exclude_test('tests/examples/spelling-checker')

# Various tests don't work under GCC 4.7
# (or rather, don't give the same output as under 4.6):
if features['GCC_PYTHON_PLUGIN_CONFIG_has_PLUGIN_FINISH_DECL']:
    # assumes it's uninitialized:
    exclude_test('tests/cpychecker/absinterp/arrays5')

    # line number differerences:
    exclude_test('tests/cpychecker/absinterp/comparisons/expressions')

    exclude_test('tests/cpychecker/refcounts/combinatorial-explosion')
    exclude_test('tests/cpychecker/refcounts/combinatorial-explosion-with-error')
    exclude_test('tests/cpychecker/refcounts/correct_object_ctor')

    # sense of a boolean is reversed:
    exclude_test('tests/cpychecker/refcounts/fold_conditional')

    # gains gcc.Function('__deleting_dtor '):
    exclude_test('tests/examples/cplusplus/classes')

    # some gimple changes:
    exclude_test('tests/plugin/array-type')

    exclude_test('tests/plugin/arrays')

    # one less output:
    exclude_test('tests/plugin/callbacks/refs')

    # changes in output:
    exclude_test('tests/plugin/dumpfiles')

    # gains: :py:class:`gcc.WidenLshiftExpr`    `w<<`
    exclude_test('tests/plugin/expressions/get_symbol')

    # gains an extra gcc.GimpleLabel():
    exclude_test('tests/plugin/gimple-cond/explicit-comparison')

    # gains an extra gcc.GimpleLabel():
    exclude_test('tests/plugin/gimple-cond/implicit-comparison')

    # various gimple changes:
    exclude_test('tests/plugin/gimple-walk-tree/dump-all')

    # gimple change:
    exclude_test('tests/plugin/gimple-walk-tree/exceptions')

    # gimple changes:
    exclude_test('tests/plugin/gimple-walk-tree/find-one')

    # various (char*) go away:
    exclude_test('tests/plugin/initializers')

    #     cc1: fatal error: pass 'ipa-profile' not found but is referenced by new pass 'my-ipa-pass'
    exclude_test('tests/plugin/new-passes')

    # -Wunitialized is now disabled by default:
    exclude_test('tests/plugin/options')

    # KeyError: 'struct-reorg-cold-struct-ratio':
    exclude_test('tests/plugin/parameters')

    # gains an extra gcc.GimpleLabel():
    exclude_test('tests/plugin/switch')

    # test_var isn't visible; see
    #   https://fedorahosted.org/gcc-python-plugin/ticket/21
    exclude_test('tests/plugin/translation-units')

if sys.version_info[:2] == (3, 3):
    # These tests don't generate the same output under 3.3:
    exclude_test('tests/cpychecker/refcounts/combinatorial-explosion')
    exclude_test('tests/cpychecker/refcounts/combinatorial-explosion-with-error')

# Tests failing with gcc 4.8:
if GCC_VERSION >= 4008:
    exclude_test('tests/cpychecker/refcounts/cplusplus/destructor')
    exclude_test('tests/cpychecker/refcounts/cplusplus/empty-function')

def run_one_test(testdir):
    try:
        sys.stdout.write('%s: ' % testdir)
        run_test(testdir)
        print('OK')
        return (testdir, 'OK', None)
    except SkipTest:
        err = sys.exc_info()[1]
        print('skipped: %s' % err.reason)
        return (testdir, 'SKIP', err.reason)
    except RuntimeError:
        err = sys.exc_info()[1]
        print('FAIL')
        print(err)
        return (testdir, 'FAIL', None)

class TestRunner:
    def __init__(self):
        self.num_passes = 0
        self.skipped_tests = []
        self.failed_tests = []

    def run_tests(self, testdirs):
        for testdir in sorted(testdirs):
            tr.handle_outcome(run_one_test(testdir))

    def run_tests_in_parallel(self, testdirs):
        pool = multiprocessing.Pool(None) # uses cpu_count
        for outcome in pool.map(run_one_test, testdirs):
            tr.handle_outcome(outcome)

    def handle_outcome(self, outcome):
        testdir, result, detail = outcome
        if result == 'OK':
            self.num_passes += 1
        elif result == 'SKIP':
            self.skipped_tests.append(testdir)
        else:
            assert result == 'FAIL'
            self.failed_tests.append(testdir)

    def print_results(self):
        def num(count, singular, plural):
            return '%i %s' % (count, singular if count == 1 else plural)

        print('%s; %s; %s' % (num(self.num_passes, "success", "successes"),
                              num(len(self.failed_tests), "failure", "failures"),
                              num(len(self.skipped_tests), "skipped", "skipped")))

tr = TestRunner()
if 1:
    tr.run_tests_in_parallel(sorted(testdirs))
else:
    tr.run_tests(sorted(testdirs))

tr.print_results()
if len(tr.failed_tests) > 0:
    print('Failed tests:')
    for test in tr.failed_tests:
        print('  %s' % test)
    sys.exit(1)
