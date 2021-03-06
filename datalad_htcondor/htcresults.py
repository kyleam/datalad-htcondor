# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Inspect and merge cluster job results"""

__docformat__ = 'restructuredtext'

import logging
import shutil
from six import (
    text_type,
)

import datalad.support.ansi_colors as ac
from datalad.interface.base import (
    Interface,
    build_doc,
)
from datalad.interface.run import (
    run_command,
    _format_cmd_shorty,
    _install_and_reglob,
    _unlock_or_remove,
    GlobbedPaths,
)
from datalad.interface.utils import eval_results
from datalad.support import json_py

from datalad.support.param import Parameter
from datalad.support.constraints import (
    EnsureNone,
    EnsureChoice,
    EnsureInt,
)
from datalad.support.exceptions import CommandError

from datalad.cmd import Runner

from datalad.dochelpers import exc_str

from datalad_revolution.dataset import (
    datasetmethod,
    require_dataset,
    EnsureDataset,
)
from datalad_htcondor.htcprepare import (
    get_submissions_dir,
)


lgr = logging.getLogger('datalad.htcondor.htcresults')


@build_doc
class HTCResults(Interface):
    """TODO
    """
    # make the custom renderer the default one, as the global default renderer
    # does not yield meaningful output for this command
    result_renderer = 'tailored'

    _params_ = dict(
        cmd=Parameter(
            args=("cmd",),
            metavar=("SUBCOMMAND",),
            nargs='?',
            doc="""""",
            constraints=EnsureChoice('list', 'merge', 'remove')),
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""specify the dataset to record the command results in.
            An attempt is made to identify the dataset based on the current
            working directory. If a dataset is given, the command will be
            executed in the root directory of this dataset.""",
            constraints=EnsureDataset() | EnsureNone()),
        submission=Parameter(
            args=("submission",),
            nargs='?',
            metavar='SUBMISSION',
            doc=""""""),
        job=Parameter(
            args=("-j", "--job",),
            metavar='NUMBER',
            doc="""""",
            constraints=EnsureInt() | EnsureNone()),
        all=Parameter(
            args=("--all",),
            action='store_true',
            doc=""""""),
    )

    @staticmethod
    @datasetmethod(name='htc_results')
    @eval_results
    def __call__(
            cmd='list',
            dataset=None,
            submission=None,
            job=None,
            all=False):

        ds = require_dataset(
            dataset,
            check_installed=True,
            purpose='handling results of remote command executions')

        if cmd == 'list':
            jw = _list_job
            sw = _list_submission
        elif cmd == 'merge':
            jw = _apply_output
            sw = None
        elif cmd == 'remove':
            if not all and not submission and not job:
                raise ValueError(
                    "use the '--all' flag to remove all results across all "
                    "submissions")
            jw = _remove_dir
            sw = _remove_dir
        else:
            raise ValueError("unknown sub-command '{}'".format(cmd))

        for res in _doit(ds, submission, job, jw, sw):
            yield res

    @staticmethod
    def custom_result_renderer(res, **kwargs):  # pragma: no cover
        from datalad.ui import ui
        if not res['status'] == 'ok' or not res['action'].startswith('htc_'):
            # logging reported already
            return
        action = res['action'].split('_')[-1]
        ui.message('{action} {sub}{job}{state}{cmd}'.format(
            action=ac.color_word(action, kw_color_map.get(action, ac.WHITE))
            if action != 'list' else '',
            sub=res['submission'],
            job=' :{}'.format(res['job']) if 'job' in res else '',
            state=' [{}]'.format(
                ac.color_word(
                    res['state'],
                    kw_color_map.get(res['state'], ac.MAGENTA))
                if res.get('state', None) else 'unknown')
            if action == 'list' else '',
            cmd=': {}'.format(
                _format_cmd_shorty(res['cmd']))
            if 'cmd' in res else '',
        ))


kw_color_map = {
    'remove': ac.RED,
    'merge': ac.GREEN,
    'completed': ac.GREEN,
    'submitted': ac.WHITE,
    'prepared': ac.YELLOW,
}


def _remove_dir(ds, dir, _ignored=None):
    common = dict(
        action='htc_result_remove',
        path=text_type(dir),
    )
    try:
        shutil.rmtree(text_type(dir))
        yield dict(
            status='ok',
            **common)
    except Exception as e:
        yield dict(
            status='error',
            message=("could not remove directory '%s': %s",
                     common['path'], exc_str(e)),
            **common)


def _list_job(ds, jdir, sdir):
    props = list(_list_submission(ds, sdir))[0]
    job_status_path = jdir / 'status'
    yield dict(
        props,
        state=job_status_path.read_text() if job_status_path.exists()
        else props.get('state', None),
        path=text_type(jdir),
    )


def _list_submission(ds, sdir):
    submission_status_path = sdir / 'status'
    args_path = sdir / 'runargs.json'
    if args_path.exists():
        try:
            cmd = json_py.load(args_path)['cmd']
        except Exception:
            cmd = None
    else:
        cmd = None
    yield dict(
        action='htc_result_list',
        status='ok',
        state=submission_status_path.read_text()
        if submission_status_path.exists() else None,
        path=text_type(sdir),
        **(dict(cmd=cmd) if cmd else {})
    )


def _apply_output(ds, jdir, sdir):
    common = dict(
        action='htc_result_merge',
        refds=text_type(ds.pathobj),
        path=text_type(jdir),
        logger=lgr,
    )
    args_path = sdir / 'runargs.json'
    try:
        # anything below PY3.6 needs stringification
        runargs = json_py.load(str(args_path))
    except Exception as e:
        yield dict(
            common,
            status='error',
            message=("could not load submission arguments from '%s': %s",
                     args_path, exc_str(e)))
        return
    # TODO check recursive status to have dataset clean
    # TODO have query limited to outputs if exlicit was given
    # prep outputs (unlock or remove)
    # COPY: this is a copy of the code from run_command
    outputs = GlobbedPaths(runargs['outputs'], pwd=runargs['pwd'],
                           expand=runargs['expand'] in ["outputs", "both"])
    if outputs:
        for res in _install_and_reglob(ds, outputs):
            yield res
        for res in _unlock_or_remove(ds, outputs.expand(full=True)):
            yield res
    # END COPY

    # TODO need to immitate PWD change, if needed
    # -> extract tarball
    try:
        stdout, stderr = Runner().run(
            ['tar', '-xf', '{}'.format(jdir / 'output')],
            cwd=ds.path)
    except CommandError as e:
        yield dict(
            common,
            status='error',
            message=("could not un-tar job results from '%s' at '%s': %s",
                     str(jdir / 'output'), ds.path, exc_str(e)))
        return

    # fake a run record, as if we would have executed locally
    for res in run_command(
            runargs['cmd'],
            dataset=ds,
            inputs=runargs['inputs'],
            outputs=runargs['outputs'],
            expand=runargs['expand'],
            explicit=runargs['explicit'],
            message=runargs['message'],
            sidecar=runargs['sidecar'],
            # TODO pwd, exit code
            extra_info=None,
            inject=True):
        yield res

    res = list(_remove_dir(ds, jdir))[0]
    res['action'] = 'htc_results_merge'
    res['status'] = 'ok'
    res.pop('message', None)
    # not removing the submission files (for now), even if the last job output
    # might be removed now. Those submissions are tiny and could be resubmitted
    yield res


def _doit(ds, submission, job, jworker, sworker):
    common = dict(
        refds=text_type(ds.pathobj),
        logger=lgr,
    )
    submissions_dir = get_submissions_dir(ds)
    if not submissions_dir.exists() or not submissions_dir.is_dir():
        return
    if submission:
        sdir = submissions_dir / 'submit_{}'.format(submission)
        if not sdir.is_dir():
            yield dict(
                action='htc_results',
                status='error',
                path=text_type(sdir),
                message=("submission '%s' does not exist", submission),
                **common)
            return
    for p in submissions_dir.iterdir() \
            if submission is None \
            else [sdir]:
        if sworker is not None and job is None:
            for res in sworker(ds, p):
                if res.get('action', '').startswith('htc_'):
                    # polish our own results
                    yield dict(
                        res,
                        submission=text_type(p.name)[7:],
                        **common)
                else:
                    # let others pass through
                    yield res
        if not p.is_dir() or not p.match('submit_*'):
            continue
        for j in p.iterdir() \
                if job is None else [p / 'job_{0:d}'.format(job)]:
            if not j.is_dir():
                continue
            for res in jworker(ds, j, p):
                if res.get('action', '').startswith('htc_'):
                    # polish our own results
                    yield dict(
                        res,
                        submission=text_type(p.name)[7:],
                        job=int(text_type(j.name)[4:]),
                        **common)
                else:
                    # let others pass through
                    yield res
