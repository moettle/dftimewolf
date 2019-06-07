# -*- coding: utf-8 -*-
"""Processes GCP cloud disks using a remote Turbinia instance."""
from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import print_function

import os
import tempfile
import time

from turbinia import client as turbinia_client
from turbinia import config as turbinia_config
from turbinia import evidence
from turbinia import output_manager
from turbinia import TurbiniaException
from turbinia.message import TurbiniaRequest

from dftimewolf.lib.module import BaseModule
from dftimewolf.lib import containers

# pylint: disable=no-member


class TurbiniaProcessor(BaseModule):
  """Process cloud disks with a remote Turbinia instance.

  Attributes:
    client: A TurbiniaClient object
    disk_name (string): Name of the disk to process
    instance (string): The name of the Turbinia instance
    project (string): The project containing the disk to process
    turbinia_region (string): The region Turbinia is in
    turbinia_zone (string): The zone Turbinia is in
    _output_path: The path to output files
  """

  def __init__(self, state):
    """Initialize the Turbinia artifact processor object.

    Args:
      state: The dfTimewolf state object
    """
    super(TurbiniaProcessor, self).__init__(state)
    self.client = None
    self.disk_name = None
    self.instance = None
    self.project = None
    self.turbinia_region = None
    self.turbinia_zone = None
    self._output_path = None

  # pylint: disable=arguments-differ
  def setup(self, disk_name, project, turbinia_zone):
    """Sets up the object attributes.

    Args:
      disk_name (string): Name of the disk to process
      project (string): The project containing the disk to process
      turbinia_zone (string): The zone containing the disk to process
    """
    # TODO: Consider the case when multiple disks are provided by the previous
    # module or by the CLI.

    if project is None or turbinia_zone is None:
      self.state.add_error(
          'project or turbinia_zone are not all specified, bailing out',
          critical=True)
      return

    self.disk_name = disk_name
    self.project = project
    self.turbinia_zone = turbinia_zone

    try:
      turbinia_config.LoadConfig()
      self.turbinia_region = turbinia_config.TURBINIA_REGION
      self.instance = turbinia_config.PUBSUB_TOPIC
      if turbinia_config.TURBINIA_PROJECT != self.project:
        self.state.add_error(
            'Specified project {0:s} does not match Turbinia configured '
            'project {1:s}. Use gcp_turbinia_import recipe to copy the disk '
            'into the same project.'.format(
                self.project, turbinia_config.TURBINIA_PROJECT), critical=True)
        return
      self._output_path = tempfile.mkdtemp()
      self.client = turbinia_client.TurbiniaClient()
    except TurbiniaException as e:
      self.state.add_error(e, critical=True)
      return

  def cleanup(self):
    pass

  def _print_task_data(self, task):
    """Pretty-prints task data.

    Args:
      task: Task dict generated by Turbinia.
    """
    print(' {0:s} ({1:s})'.format(task['name'], task['id']))
    paths = task.get('saved_paths', [])
    if not paths:
      return
    for path in paths:
      if path.endswith('worker-log.txt'):
        continue
      if path.endswith('{0:s}.log'.format(task.get('id'))):
        continue
      if path.startswith('/'):
        continue
      print('   ' + path)

  def display_task_progress(
      self, instance, project, region, request_id=None, user=None,
      poll_interval=60):
    """Displays the overall progress of tasks in a Turbinia job.

    Args:
      instance (string): The name of the Turbinia instance
      project (string): The project containing the disk to process
      region (string): Region where turbinia is configured.
      request_id (string): The request ID provided by Turbinia.
      user (string): The username to filter tasks by.
      poll_interval (int): The interval at which to poll for new results.
    """
    total_completed = 0

    while True:
      task_results = self.client.get_task_data(
          instance, project, region, request_id=request_id, user=user)
      tasks = {task['id']: task for task in task_results}
      completed_tasks = set()
      pending_tasks = set()

      for task in tasks.values():
        if task.get('successful') is not None:
          completed_tasks.add(task['id'])
        else:
          pending_tasks.add(task['id'])

      if len(completed_tasks) > total_completed or not completed_tasks:
        total_completed = len(completed_tasks)

        print('Task status update (completed: {0:d} | pending: {1:d})'.format(
            len(completed_tasks), len(pending_tasks)))

        print('Completed tasks:')
        for task_id in completed_tasks:
          self._print_task_data(tasks[task_id])

        print('Pending tasks:')
        for task_id in pending_tasks:
          self._print_task_data(tasks[task_id])

      if len(completed_tasks) == len(task_results) and completed_tasks:
        print('All {0:d} Tasks completed'.format(len(task_results)))
        return

      time.sleep(poll_interval)

  def process(self):
    """Process files with Turbinia."""
    log_file_path = os.path.join(self._output_path, 'turbinia.log')
    print('Turbinia log file: {0:s}'.format(log_file_path))

    if self.state.input and not self.disk_name:
      _, disk = self.state.input[0]
      self.disk_name = disk.name
      print('Using disk {0:s} from previous collector'.format(self.disk_name))

    evidence_ = evidence.GoogleCloudDisk(
        disk_name=self.disk_name, project=self.project, zone=self.turbinia_zone)
    request = TurbiniaRequest()
    request.evidence.append(evidence_)

    # Get threat intelligence data from any modules that have stored some.
    # In this case, observables is a list of containers.ThreatIntelligence
    # objects.
    threatintel = self.state.get_containers(containers.ThreatIntelligence)
    if threatintel:
      print('Sending {0:d} threatintel to Turbinia GrepWorkers...'.format(
          len(threatintel)))
      indicators = [item.indicator for item in threatintel]
      request.recipe['filter_patterns'] = indicators

    request_dict = {
        'instance': self.instance,
        'project': self.project,
        'region': self.turbinia_region,
        'request_id': request.request_id
    }

    try:
      print('Creating Turbinia request {0:s} with Evidence {1!s}'.format(
          request.request_id, evidence_.name))
      self.client.send_request(request)
      print('Waiting for Turbinia request {0:s} to complete'.format(
          request.request_id))
      self.client.wait_for_request(**request_dict)
      task_data = self.client.get_task_data(**request_dict)
    except TurbiniaException as e:
      self.state.add_error(e, critical=True)
      return

    # Turbinia run complete, build a human-readable message of results.
    message = 'Completed {0:d} Turbinia tasks\n'.format(len(task_data))
    for task in task_data:
      message += '{0!s} ({1!s}): {2!s}\n'.format(
          task.get('name'),
          task.get('id'),
          task.get('status', 'No task status'))
      # saved_paths may be set to None
      for path in task.get('saved_paths') or []:
        if path.endswith('worker-log.txt'):
          continue
        if path.endswith('{0!s}.log'.format(task.get('id'))):
          continue
        if path.startswith('/'):
          continue
        message += '  {0:s}\n'.format(path)
    print(message)

    # Store the message for consumption by any reporting modules.
    report = containers.Report(module_name='TurbiniaProcessor', text=message)
    self.state.store_container(report)

    # This finds all .plaso files in the Turbinia output, and determines if they
    # are local or remote (it's possible this will be running against a local
    # instance of Turbinia).
    local_paths = []
    gs_paths = []
    timeline_label = '{0:s}-{1:s}'.format(self.project, self.disk_name)
    for task in task_data:
      # saved_paths may be set to None
      for path in task.get('saved_paths') or []:
        if path.startswith('/') and path.endswith('.plaso'):
          local_paths.append(path)
        if path.startswith('gs://') and path.endswith('.plaso'):
          gs_paths.append(path)

    if not local_paths and not gs_paths:
      self.state.add_error(
          'No .plaso files found in Turbinia output.', critical=True)
      return

    # Any local .plaso files that exist we can add immediately to the output
    self.state.output = [
        (timeline_label, p) for p in local_paths if os.path.exists(p)]

    # For files remote in GCS we copy each plaso file back from GCS and then add
    # to output paths
    # TODO: Externalize fetching files from GCS buckets to a different module.
    for path in gs_paths:
      local_path = None
      try:
        output_writer = output_manager.GCSOutputWriter(
            path, local_output_dir=self._output_path)
        local_path = output_writer.copy_from(path)
      except TurbiniaException as e:
        self.state.add_error(e, critical=True)
        return

      if local_path:
        self.state.output.append((timeline_label, local_path))

    if not self.state.output:
      self.state.add_error('No .plaso files could be found.', critical=True)
