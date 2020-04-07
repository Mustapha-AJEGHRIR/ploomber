"""
Task.exec_status only makes sense for parallel execution, when using the serial
executor, we can update the _status automatically (within Task) but it is
unusable, get rid of the logic that updates it automatically, however,
we ar eusing here the _update status here to propagate dowstream updates.
So think about which parts can go away and which ones should not
"""
import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from ploomber.constants import TaskStatus
from ploomber.executors.Executor import Executor
from ploomber.executors.LoggerHandler import LoggerHandler
from ploomber.exceptions import DAGBuildError
from ploomber.ExceptionCollector import ExceptionCollector, ExceptionResult

import traceback


class TaskBuildWrapper:
    """
    Wraps task.build. Traceback is lost when using ProcessPoolExecutor
    (future.exception().__traceback__ returns None), so we have to catch it
    here and return it so at the end of the DAG execution, a message with the
    traceback can be printed
    """

    def __init__(self, task):
        self.task = task

    def __call__(self, *args, **kwargs):
        try:
            return self.task.build(**kwargs)
        except Exception as e:
            return ExceptionResult(task_str=repr(self.task),
                                   traceback_str=traceback.format_exc())


class Parallel(Executor):
    """Runs a DAG in parallel using multiprocessing

    Notes
    -----
    If any task crashes, downstream tasks execution is aborted, building
    continues until no more tasks can be executed
    """
    # NOTE: Tasks should not create child processes
    # https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.daemon

    def __init__(self, processes=4, logging_directory=None,
                 logging_level=logging.INFO):
        self.logging_directory = logging_directory
        self.logging_level = logging_level
        self.processes = processes

        self._logger = logging.getLogger(__name__)
        self._i = 0

    def __call__(self, dag, **kwargs):
        super().__call__(dag)

        if self.logging_directory:
            logger_handler = LoggerHandler(dag_name=dag.name,
                                           directory=self.logging_directory,
                                           logging_level=self.logging_level)
            logger_handler.add()
        # TODO: Have to test this with other Tasks, especially the ones that use
        # clients - have to make sure they are serialized correctly
        done = []
        started = []
        set_all = set(dag)
        future_mapping = {}

        # there might be up-to-date tasks, add them to done
        # FIXME: this only happens when the dag is already build and then
        # then try to build again (in the same session), if the session
        # is restarted even up-to-date tasks will be WaitingExecution again
        # this is a bit confusing, so maybe change WaitingExecution
        # to WaitingBuild?
        for name in dag:
            if dag[name].exec_status in {TaskStatus.Executed,
                                         TaskStatus.Skipped}:
                done.append(dag[name])

        def callback(future):
            """Keep track of finished tasks
            """
            task = future_mapping[future]
            self._logger.debug('Added %s to the list of finished tasks...',
                               task.name)
            try:
                result = future.result()
            except BrokenProcessPool as e:
                # ignore the error here but flag the task,
                # so next_task is able to stop the iteration,
                # when we call result after breaking the loop,
                # this will show up
                task.exec_status = TaskStatus.BrokenProcessPool
            else:
                if isinstance(result, ExceptionResult):
                    task.exec_status = TaskStatus.Errored
                else:
                    task.exec_status = result

            done.append(task)

        def next_task():
            """
            Return the next Task to execute, returns None if no Tasks are available
            for execution (cause their dependencies are not done yet) and raises
            a StopIteration exception if there are no more tasks to run, which means
            the DAG is done
            """
            for task in dag.values():
                if task.exec_status in {TaskStatus.Aborted}:
                    done.append(task)
                elif task.exec_status == TaskStatus.BrokenProcessPool:
                    raise StopIteration

            # iterate over tasks to find which is ready for execution
            for task in dag.values():
                # ignore tasks that are already started, I should probably add an
                # executing status but that cannot exist in the task itself,
                # maybe in the manaer?
                if (task.exec_status == TaskStatus.WaitingExecution
                        and task not in started):
                    return task
                # there might be some up-to-date tasks, add them

            set_done = set([t.name for t in done])

            if not self._i % 1000:
                self._logger.debug('Finished tasks so far: %s', set_done)
                self._logger.debug('Remaining tasks: %s',
                                   set_all - set_done)
                self._logger.info('Finished %i out of %i tasks',
                                  len(set_done), len(set_all))

            if set_done == set_all:
                self._logger.debug('All tasks done')

                if self.logging_directory:
                    logger_handler.remove()

                raise StopIteration

            self._i += 1

        with ProcessPoolExecutor(max_workers=self.processes) as pool:
            while True:
                try:
                    task = next_task()
                except StopIteration:
                    break
                else:
                    if task is not None:
                        future = pool.submit(TaskBuildWrapper(task), **kwargs)
                        future.add_done_callback(callback)
                        started.append(task)
                        future_mapping[future] = task
                        logging.info('Added %s to the pool...', task.name)
                        # time.sleep(3)

        exps = ExceptionCollector([get_future_result(f, future_mapping)
                                   for f, t
                                   in future_mapping.items()
                                   if isinstance(get_future_result(f,
                                                                   future_mapping),
                                                 ExceptionResult)])

        if exps:
            raise DAGBuildError('DAG build failed, the following '
                                'tasks crashed '
                                '(corresponding downstream tasks aborted '
                                'execution):\n{}'
                                .format(str(exps)))

    # __getstate__ and __setstate__ are needed to make this picklable

    def __getstate__(self):
        state = self.__dict__.copy()
        # _logger is not pickable, so we remove them and build
        # them again in __setstate__
        del state['_logger']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._logger = logging.getLogger(__name__)


def get_future_result(future, future_mapping):
    try:
        return future.result()
    except BrokenProcessPool as e:
        raise BrokenProcessPool('Broken pool {}'.format(
            future_mapping[future])) from e
