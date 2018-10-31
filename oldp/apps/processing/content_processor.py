import glob
import logging.config
import os
from enum import Enum
from importlib import import_module
from typing import List

from django.conf import settings
from django.db.models import Model

from oldp.apps.processing.errors import ProcessingError
from oldp.apps.processing.processing_steps import BaseProcessingStep

ContentStorage = Enum('ContentStorage', 'ES FS DB')

logger = logging.getLogger(__name__)


class InputHandler(object):
    input_selector = None  # Can be single, list, ... depends on get_content
    input_limit = 0  # 0 = unlimited
    input_start = 0
    pre_processed_content = []

    def __init__(self, limit=0, start=0, selector=None):
        self.input_limit = limit
        self.input_selector = selector
        self.input_start = start

    def handle_input(self, input_content) -> None:
        raise NotImplementedError()

    def get_input(self) -> list:
        raise NotImplementedError()


class InputHandlerFS(InputHandler):
    """Read content files for initial processing from file system"""
    dir_selector = '/*'

    def get_input_content_from_selector(self, selector) -> list:
        content = []

        if isinstance(selector, str):
            if os.path.isdir(selector):
                # Get all files recursive
                content.extend(sorted(file for file in glob.glob(selector + self.dir_selector, recursive=True)))
            elif os.path.isfile(selector):
                # Selector is specific file
                content.append(selector)
        elif isinstance(selector, list):
            # List of selectors
            for s in selector:
                content.extend(self.get_input_content_from_selector(s))
        return content

    def get_input(self) -> List[str]:
        """Select files from input_selector recursively and from directory with dir_selector """

        if self.input_selector is None:
            raise ProcessingError('input_selector is not set')

        content_list = self.get_input_content_from_selector(self.input_selector)[self.input_start:]

        if len(content_list) < 1:
            raise ProcessingError('Input selector is empty: %s' % self.input_selector)

        if self.input_limit > 0:
            content_list = content_list[:self.input_limit]

        return content_list

    def handle_input(self, input_content: str) -> None:
        raise NotImplementedError()


class ContentProcessor(object):
    """Base class for content processing pipeline

    Methods are called in the following order:

    1. get_input: returns list of input objects (fs: file path, db: model instance)
        - fs: set_input: list of dirs or files
        - db: set_input: db.queryset
    2. handle_input: handles input objects and transforms them to processing objects (fs: file path > model instance
        + save instance, db: keep model instance); write to self.pre_processed_content
    3. process: iterate over all processing steps (model instance > model instance), save processed model (in db
        + self.processed_content)
    4. post_process: iterate over all post processing steps (e.g. write to ES)

    """
    model = None # type: Model
    working_dir = os.path.join(settings.BASE_DIR, 'workingdir')

    input_handler = None  # type: InputHandler

    processed_content = []
    pre_processed_content = []
    available_processing_steps = None  # type: dict
    processing_steps = []
    post_processing_steps = []

    # Errors
    pre_processing_errors = []
    post_processing_errors = []
    processing_errors = []

    # Storage
    # output_path = 'http://localhost:9200'

    # DB settings (Django db models to be deleted on setup)
    # db_models = []

    # Stats
    file_counter = 0
    file_failed_counter = 0
    doc_counter = 0
    doc_failed_counter = 0

    def __init__(self):
        # Working dir
        self.processing_steps = []  # type: List[BaseProcessingStep]
        self.processed_content = []
        self.pre_processed_content = []
        self.pre_processing_errors = []
        self.post_processing_errors = []
        self.processing_errors = []

    def set_parser_arguments(self, parser):
        # Enable arguments that are used by all children
        parser.add_argument('--verbose', action='store_true', default=False)

        parser.add_argument('step', nargs='*', type=str, help='Processing steps', default='all',
                            choices=list(self.get_available_processing_steps().keys()) + ['all'])

    def set_options(self, options):
        # Set options according to parser options
        # self.output_path = options['output']

        if options['verbose']:
            logger.setLevel(logging.DEBUG)

    def empty_content(self):
        raise NotImplementedError()

    def set_input_handler(self, handler: InputHandler):
        self.input_handler = handler

    def call_processing_steps(self, content):
        """Call each processing step one by one"""
        for step in self.processing_steps:  # type: BaseProcessingStep
            try:
                content = step.process(content)
            except ProcessingError as e:
                logger.error('Failed to call processing step (%s): %s' % (step, e))
                self.processing_errors.append(e)
        return content

    def set_processing_steps(self, step_list):
        """Selects processings steps from available dict"""

        # Unset old steps and load available steps
        self.processing_steps = []
        self.get_available_processing_steps()

        if not isinstance(step_list, List):
            step_list = [step_list]

        if 'all' in step_list:
            return self.available_processing_steps.values()

        for step in step_list:
            if step in self.available_processing_steps:
                self.processing_steps.append(self.available_processing_steps[step])
            else:
                raise ProcessingError('Requested step is not available: %s' % step)

    def get_available_processing_steps(self) -> dict:
        """Loads available processing steps based on package names in settings"""
        if self.available_processing_steps is None:
            self.available_processing_steps = {}

            # Get packages for model type
            for step_package in settings.PROCESSING_STEPS[self.model.__name__]:  # type: str
                module = import_module(step_package)

                if 'ProcessingStep' not in module.__dict__:
                    raise ProcessingError('Processing step package does not contain "ProcessingStep" class: %s' % step_package)

                step_cls = module.ProcessingStep()  # type: BaseProcessingStep

                if not isinstance(step_cls, BaseProcessingStep):
                    raise ProcessingError('Processing step needs to inherit from BaseProcessingStep: %s' % step_package)

                step_name = step_package.split('.')[-1]  # last module name from package path

                # Write to dict
                self.available_processing_steps[step_name] = step_cls

        return self.available_processing_steps

    def process(self):

        # Reset queues
        self.pre_processed_content = []
        self.processed_content = []

        # Separate input handling and processing (processing needs to access previous items)
        self.input_handler.pre_processed_content = []
        for input_content in self.input_handler.get_input():
            try:
                self.input_handler.handle_input(input_content)
            except ProcessingError as e:
                logger.error('Failed to process content (%s): %s' % (input_content, e))
                self.pre_processing_errors.append(e)
        self.pre_processed_content = self.input_handler.pre_processed_content

        logger.debug('Pre-processed content: %i' % len(self.pre_processed_content))

        # Start actual processing
        self.process_content()

        # Call post processing steps (each with whole content queue)
        for step in self.post_processing_steps:
            try:
                step.process(self.processed_content)
            except ProcessingError as e:
                logger.error('Failed to call post processing step (%s): %s' % (step, e))
                self.post_processing_errors.append(e)

    def process_content(self):
        raise NotImplementedError("Child class instead to implement this method.")

    def log_stats(self):
        logger.info('Processing stats:')
        logger.info('- Successful files: %i (failed: %i)' % (self.file_counter, self.file_failed_counter))
        logger.info('- Successful documents: %i (failed: %i)' % (self.doc_counter, self.doc_failed_counter))

        for step in self.post_processing_steps:
            if hasattr(step, 'log_stats'):
                step.log_stats()

        if len(self.pre_processing_errors) > 0:
            logger.warning('Pre-processing errors: %i' % len(self.pre_processing_errors))
            logger.debug('Pre-processing errors: %s' % self.pre_processing_errors)

        if len(self.processing_errors) > 0:
            logger.warning('Processing errors: %i' % len(self.processing_errors))
            logger.debug('Processing errors: %s' % self.processing_errors)

        if len(self.post_processing_errors) > 0:
            logger.warning('Post-processing errors: %i' % len(self.post_processing_errors))
            logger.debug('Post-processing errors: %s' % self.post_processing_errors)

