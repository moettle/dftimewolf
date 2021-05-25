# -*- coding: utf-8 -*-
"""Export processing results to Timesketch."""

import re
import time

from timesketch_import_client import importer

from dftimewolf.lib import module
from dftimewolf.lib import timesketch_utils
from dftimewolf.lib.containers import containers
from dftimewolf.lib.modules import manager as modules_manager


class TimesketchExporter(module.BaseModule):
  """Exports a given set of plaso or CSV files to Timesketch.

  input: A list of paths to plaso or CSV files.
  output: A URL to the generated timeline.

  Attributes:
    incident_id (str): Incident ID or reference. Used in sketch description.
    sketch_id (int): Sketch ID to add the resulting timeline to. If not
        provided, a new sketch is created.
    timesketch_api (TimesketchApiClient): Timesketch API client.
  """

  # The name of a ticket attribute that contains the URL to a sketch.
  _SKETCH_ATTRIBUTE_NAME = 'Timesketch URL'

  def __init__(self, state, name=None, critical=False):
    super(TimesketchExporter, self).__init__(
        state, name=name, critical=critical)
    self.incident_id = None
    self.sketch_id = None
    self.timesketch_api = None
    self._analyzers = []
    self.wait_for_timelines = False

  def SetUp(self,  # pylint: disable=arguments-differ
            incident_id=None,
            sketch_id=None,
            analyzers=None,
            token_password='',
            wait_for_timelines=False):
    """Setup a connection to a Timesketch server and create a sketch if needed.

    Args:
      incident_id (Optional[str]): Incident ID or reference. Used in sketch
          description.
      sketch_id (Optional[str]): Sketch ID to add the resulting timeline to.
          If not provided, a new sketch is created.
      analyzers (Optional[List[str]): If provided a list of analyzer names
          to run on the sketch after they've been imported to Timesketch.
      token_password (str): optional password used to decrypt the
          Timesketch credential storage. Defaults to an empty string since
          the upstream library expects a string value. An empty string means
          a password will be generated by the upstream library.
      wait_for_timelines (bool): Whether to wait until timelines are processed
          in the Timesketch server or not.
    """
    self.wait_for_timelines = bool(wait_for_timelines)

    self.timesketch_api = timesketch_utils.GetApiClient(
        self.state, token_password=token_password)
    if not self.timesketch_api:
      self.ModuleError(
          'Unable to get a Timesketch API client, try deleting the files '
          '~/.timesketchrc and ~/.timesketch.token', critical=True)
    self.incident_id = incident_id
    self.sketch_id = int(sketch_id) if sketch_id else None
    sketch = None

    # Check that we have a timesketch session.
    if not (self.timesketch_api or self.timesketch_api.session):
      message = 'Could not connect to Timesketch server'
      self.ModuleError(message, critical=True)

    # If no sketch ID is provided through the CLI, attempt to get it from
    # attributes
    if not self.sketch_id:
      self.sketch_id = self._GetSketchIDFromAttributes()

    # If we have a sketch ID, check that we can write to it and cache it.
    if self.sketch_id:
      sketch = self.timesketch_api.get_sketch(self.sketch_id)
      if 'write' not in sketch.my_acl:
        self.ModuleError(
            'No write access to sketch ID {0:d}, aborting'.format(self.sketch_id),
            critical=True)
      self.state.AddToCache('timesketch_sketch', sketch)
      self.sketch_id = sketch.id

    if analyzers and isinstance(analyzers, (tuple, list)):
      self._analyzers = analyzers

  def _CreateSketch(self, incident_id=None):
    """Creates a new Timesketch sketch.

    Args:
      incident_id (str): Incident ID to use sketch description.

    Returns:
      timesketch_api_client.Sketch: An instance of the sketch object.
    """
    if incident_id:
      sketch_name = 'Sketch for incident ID: ' + incident_id
    else:
      sketch_name = 'Untitled sketch'
    sketch_description = 'Sketch generated by dfTimewolf'

    sketch = self.timesketch_api.create_sketch(
        sketch_name, sketch_description)
    self.sketch_id = sketch.id
    if incident_id:
      sketch.add_attribute(
          'incident_id', incident_id, ontology='text')
    self.state.AddToCache('timesketch_sketch', sketch)

    return sketch

  def _WaitForTimelines(self):
    """Waits for all timelines in a sketch to be processed."""
    time.sleep(5)  # Give Timesketch time to populate recently added timelines.
    sketch = self.timesketch_api.get_sketch(self.sketch_id)
    timelines = sketch.list_timelines()
    while True:
      if all(tl.status in ['fail', 'ready', 'timeout', 'archived']
             for tl in timelines):
        break
      time.sleep(10)

  def _GetSketchIDFromAttributes(self):
    """Attempts to retrieve a Timesketch ID from ticket attributes.

    Returns:
      int: the sketch idenifier, or None if one was not available.
    """
    attributes = self.state.GetContainers(containers.TicketAttribute)
    for attribute in attributes:
      if attribute.name == self._SKETCH_ATTRIBUTE_NAME:
        sketch_match = re.search(r'sketch/(\d+)/', attribute.value)
        if sketch_match:
          sketch_id = int(sketch_match.group(1), 10)
          return sketch_id
    return None

  def Process(self):
    """Executes a Timesketch export."""
    if not self.timesketch_api:
      message = 'Could not connect to Timesketch server'
      self.ModuleError(message, critical=True)

    sketch = self.state.GetFromCache('timesketch_sketch')
    if not sketch and self.sketch_id:
      self.logger.info('Using exiting sketch: {0:d}'.format(self.sketch_id))
      sketch = self.timesketch_api.get_sketch(self.sketch_id)

    # Create the sketch if no sketch was stored in the cache.
    if not sketch:
      sketch = self._CreateSketch(incident_id=self.incident_id)
      self.logger.info('New sketch created: {0:d}'.format(self.sketch_id))

    recipe_name = self.state.recipe.get('name', 'no_recipe')
    input_names = []
    for file_container in self.state.GetContainers(containers.File):
      description = file_container.name
      if not description:
        continue
      name = description.rpartition('.')[0]
      name = name.replace(' ', '_').replace('-', '_')
      input_names.append(name)

    if input_names:
      timeline_name = '{0:s}_{1:s}'.format(
          recipe_name, '_'.join(input_names))
    else:
      timeline_name = recipe_name

    with importer.ImportStreamer() as streamer:
      streamer.set_sketch(sketch)
      streamer.set_timeline_name(timeline_name)

      for file_container in self.state.GetContainers(containers.File):
        path = file_container.path
        description = file_container.description
        streamer.add_file(path)
        if streamer.response and description:
          streamer.timeline.description = description

    api_root = sketch.api.api_root
    host_url = api_root.partition('api/v1')[0]
    sketch_url = '{0:s}sketches/{1:d}/'.format(host_url, sketch.id)
    message = 'Your Timesketch URL is: {0:s}'.format(sketch_url)
    self.logger.info(message)
    container = containers.Report(
        module_name='TimesketchExporter',
        text=message,
        text_format='markdown')
    self.state.StoreContainer(container)

    if self.wait_for_timelines:
      self.logger.info('Waiting for timelines to finish processing...')
      self._WaitForTimelines()

    for analyzer in self._analyzers:
      results = sketch.run_analyzer(
          analyzer_name=analyzer, timeline_name=timeline_name)
      if not results:
        self.logger.info('Analyzer [{0:s}] not able to run on {1:s}'.format(
            analyzer, timeline_name))
        continue
      session_id = results.id
      if not session_id:
        self.logger.info(
            'Analyzer [{0:s}] didn\'t provide any session data'.format(
                analyzer))
        continue
      self.logger.info('Analyzer: {0:s} is running, session ID: {1:d}'.format(
          analyzer, session_id))
      self.logger.info(results.status_string)


modules_manager.ModulesManager.RegisterModule(TimesketchExporter)
