"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals, absolute_import

import logging

import docker
from flexmock import flexmock
import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.utils.rpm import parse_rpm_output
from tests.constants import DOCKERFILE_GIT
from tests.docker_mock import mock_docker
from tests.stubs import StubInsideBuilder, StubSource

TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}


PACKAGE_LIST = ['python-docker-py;1.3.1;1.fc24;noarch;(none);'
                '191456;7c1f60d8cde73e97a45e0c489f4a3b26;1438058212;(none);(none)',
                'fedora-repos-rawhide;24;0.1;noarch;(none);'
                '2149;d41df1e059544d906363605d47477e60;1436940126;(none);(none)',
                'gpg-pubkey-doc;1.0;1;noarch;(none);'
                '1000;00000000000000000000000000000000;1436940126;(none);(none)']
PACKAGE_LIST_WITH_AUTOGENERATED = PACKAGE_LIST + ['gpg-pubkey;qwe123;zxcasd123;(none);(none);0;'
                                                  '(none);1370645731;(none);(none)']
PACKAGE_LIST_WITH_AUTOGENERATED_B = [x.encode("utf-8") for x in PACKAGE_LIST_WITH_AUTOGENERATED]


pytestmark = pytest.mark.usefixtures('user_params')


def mock_logs(cid, **kwargs):
    return b"\n".join(PACKAGE_LIST_WITH_AUTOGENERATED_B)


def mock_logs_raise(cid, **kwargs):
    raise RuntimeError


def mock_logs_empty(cid, **kwargs):
    return ''


def setup_mock_logs_retry(cache=None):
    cache = cache or {}
    cache.setdefault('attempt', 0)

    def mock_logs_retry(cid, **kwargs):
        if cache['attempt'] < 4:
            logs = mock_logs_empty(cid, **kwargs)
        else:
            logs = mock_logs(cid, **kwargs)

        cache['attempt'] += 1
        return logs

    return mock_logs_retry


def get_builder(workflow, base_from_scratch=False):
    workflow.builder = StubInsideBuilder().for_workflow(workflow)
    if base_from_scratch:
        workflow.builder.set_dockerfile_images(['scratch'])
    else:
        workflow.builder.set_dockerfile_images([])
    return workflow.builder


@pytest.mark.parametrize('base_from_scratch', [True, False])
@pytest.mark.parametrize('remove_container_error', [True, False])
@pytest.mark.parametrize("ignore_autogenerated", [
    {"ignore": True, "package_list": PACKAGE_LIST},
    {"ignore": False, "package_list": PACKAGE_LIST_WITH_AUTOGENERATED},
])
def test_rpmqa_plugin(caplog, docker_tasker, base_from_scratch, remove_container_error,
                      ignore_autogenerated):
    should_raise_error = {}
    if remove_container_error:
        should_raise_error['remove_container'] = None
    mock_docker(should_raise_error=should_raise_error)

    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = get_builder(workflow, base_from_scratch)

    flexmock(docker.APIClient, logs=mock_logs)
    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{"name": PostBuildRPMqaPlugin.key,
          "args": {
              'image_id': TEST_IMAGE,
              "ignore_autogenerated_gpg_keys": ignore_autogenerated["ignore"]}}
         ])
    results = runner.run()
    if base_from_scratch:
        log_msg = "from scratch can't run rpmqa"
        assert log_msg in caplog.text
        assert results[PostBuildRPMqaPlugin.key] is None
        assert workflow.image_components is None
    else:
        assert results[PostBuildRPMqaPlugin.key] == ignore_autogenerated["package_list"]
        assert workflow.image_components == parse_rpm_output(ignore_autogenerated["package_list"])


def test_rpmqa_plugin_skip(docker_tasker):  # noqa
    """
    Test skipping the plugin if workflow.image_components is already set
    """
    mock_docker()
    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = get_builder(workflow)

    image_components = {
        'type': 'rpm',
        'name': 'something'
    }
    setattr(workflow, 'image_components', image_components)

    flexmock(docker.APIClient, logs=mock_logs_raise)
    runner = PostBuildPluginsRunner(docker_tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    results = runner.run()
    assert results[PostBuildRPMqaPlugin.key] is None
    assert workflow.image_components == image_components


def test_rpmqa_plugin_exception(docker_tasker):  # noqa
    mock_docker()
    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = get_builder(workflow)

    flexmock(docker.APIClient, logs=mock_logs_raise)
    runner = PostBuildPluginsRunner(docker_tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    with pytest.raises(PluginFailedException):
        runner.run()


def test_dangling_volumes_removed(docker_tasker, caplog):

    mock_docker()
    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = get_builder(workflow)

    runner = PostBuildPluginsRunner(docker_tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])

    runner.run()

    logs = {}
    for record in caplog.records:
        logs.setdefault(record.levelno, []).append(record.message)

    assert "container_id = 'f8ee920b2db5e802da2583a13a4edbf0523ca5fff6b6d6454c1fd6db5f38014d'" \
        in logs[logging.DEBUG]

    expected_volumes = [u'test', u'conflict_exception', u'real_exception']
    assert "volumes = {}".format(expected_volumes) in logs[logging.DEBUG]
    for volume in expected_volumes:
        assert "removing volume '{}'".format(volume) in logs[logging.INFO]
    assert 'ignoring a conflict when removing volume conflict_exception' in logs[logging.DEBUG]


def test_empty_logs_retry(docker_tasker):  # noqa
    mock_docker()
    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = get_builder(workflow)

    mock_logs_retry = setup_mock_logs_retry()
    flexmock(docker.APIClient, logs=mock_logs_retry)
    runner = PostBuildPluginsRunner(docker_tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    results = runner.run()
    assert results[PostBuildRPMqaPlugin.key] == PACKAGE_LIST
    assert workflow.image_components == parse_rpm_output(PACKAGE_LIST)


def test_empty_logs_failure(docker_tasker):  # noqa
    mock_docker()
    workflow = DockerBuildWorkflow(source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = get_builder(workflow)

    flexmock(docker.APIClient, logs=mock_logs_empty)
    runner = PostBuildPluginsRunner(docker_tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    with pytest.raises(PluginFailedException) as exc_info:
        runner.run()
    assert 'Unable to gather list of installed packages in container' in str(exc_info.value)
