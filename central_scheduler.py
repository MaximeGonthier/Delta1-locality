import time
import logging
from collections import defaultdict

from funcx import FuncXClient
from funcx.serialize import FuncXSerializer
from utils import colored
from strategies import init_strategy
from predictors import init_runtime_predictor


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(
    colored("[SCHEDULER] %(message)s", 'yellow')))
logger.addHandler(ch)


class CentralScheduler(object):

    def __init__(self, fxc=None, endpoints=None, strategy='round-robin',
                 runtime_predictor='rolling-average', last_n=3, train_every=1,
                 log_level='INFO', *args, **kwargs):
        self._fxc = fxc or FuncXClient(*args, **kwargs)

        # List of all FuncX endpoints we can execute on
        self._endpoints = list(set(endpoints or []))

        # Track which endpoints a function can't run on
        self._blacklists = defaultdict(set)

        # Track pending tasks
        self._pending = {}
        self._pending_by_endpoint = defaultdict(set)
        self._last_task_ETA = {}
        # Estimated error in the pending-task time of an endpoint.
        # Updated every time a task result is received from an endpoint.
        self._queue_error = defaultdict(float)
        # TODO: backup tasks?

        # Set logging levels
        logger.setLevel(log_level)

        # Intialize serializer
        self.fx_serializer = FuncXSerializer()
        self.fx_serializer.use_custom('03\n', 'code')

        # Initialize runtime predictor
        self.predictor = init_runtime_predictor(runtime_predictor,
                                                endpoints=endpoints,
                                                last_n=last_n,
                                                train_every=train_every)
        logger.info(f"Runtime predictor using strategy {runtime_predictor}")

        # Initialize scheduling strategy
        self.strategy = init_strategy(strategy, endpoints=self._endpoints,
                                      runtime_predictor=self.predictor,
                                      queue_predictor=self.queue_delay)
        logger.info(f"Scheduler using strategy {strategy}")

    def blacklist(self, func, endpoint):
        # TODO: use blacklists in scheduling
        if endpoint not in self._endpoints:
            logger.error('Cannot blacklist unknown endpoint {}'
                         .format(endpoint))
        else:
            logger.info('Blacklisting endpoint {} for function {}'
                        .format(endpoint, func))
            self._blacklists[func].add(endpoint)

        # TODO: return response message?

    def choose_endpoint(self, func, payload):
        choice = self.strategy.choose_endpoint(func, payload)
        logger.debug('Choosing endpoint {} for func {}'
                     .format(choice['endpoint'], func))
        choice['time_sent'] = time.time()
        choice['ETA'] = choice.get('ETA', time.time())

        # Record endpoint ETA for queue-delay prediction
        self._last_task_ETA[choice['endpoint']] = choice['ETA']
        return choice

    def log_submission(self, func, payload, choice, task_id):
        endpoint = choice['endpoint']

        logger.info('Sending func {} to endpoint {} with task id {}'
                    .format(func, endpoint, task_id))
        # logger.info('Task ETA is {:.2f} s from now'
        # .format(expected_ETA - time.time()))

        info = {
            'time_sent': choice['time_sent'],
            'ETA': choice['ETA'],
            'function_id': func,
            'endpoint_id': endpoint,
            'payload': payload,
        }
        self._pending[task_id] = info
        self._pending_by_endpoint[endpoint].add(task_id)

        return endpoint

    def log_status(self, task_id, data):
        if task_id not in self._pending:
            logger.warn('Ignoring unknown task id {}'.format(task_id))
            return

        if 'result' in data:
            result = self.fx_serializer.deserialize(data['result'])
            runtime = result['runtime']
            logger.info('Got result from {} for task {} with time {}'
                        .format(self._pending[task_id]['endpoint_id'],
                                task_id, runtime))

            self.predictor.update(self._pending[task_id], runtime)
            self._record_completed(task_id)

        elif 'exception' in data:
            exception = self.fx_serializer.deserialize(data['exception'])
            try:
                exception.reraise()
            except Exception as e:
                logger.error('Got exception on task {}: {}'
                             .format(task_id, e))

            self._record_completed(task_id)

        elif 'status' in data and data['status'] == 'PENDING':
            pass

        else:
            logger.error('Unexpected status message: {}'.format(data))

    def queue_delay(self, endpoint):
        # If there are no pending tasks on endpoint, no queue delay.
        # Otherwise, queue delay is the ETA of most recent task,
        # plus the estimated error in the ETA prediction.
        if len(self._pending_by_endpoint[endpoint]) == 0:
            delay = time.time()
        else:
            delay = self._last_task_ETA[endpoint] + self._queue_error[endpoint]
            delay = max(delay, time.time())

        return delay

    def _record_completed(self, task_id):
        info = self._pending[task_id]
        endpoint = info['endpoint_id']

        # If this is the last pending task on this endpoint, reset ETA offset
        if len(self._pending_by_endpoint[endpoint]) == 1:
            self._queue_error[endpoint] = 0.0
            del self._last_task_ETA[endpoint]
        else:
            prediction_error = time.time() - self._pending[task_id]['ETA']
            self._queue_error[endpoint] = prediction_error
            # print(colored(f'Prediction error {prediction_error}', 'red'))

        logger.info('Task exec time: expected = {:.3f}, actual = {:.3f}'
                    .format(info['ETA'] - info['time_sent'],
                            time.time() - info['time_sent']))
        # logger.info(f'ETA_offset = {self._queue_error[endpoint]:.3f}')

        del self._pending[task_id]
        self._pending_by_endpoint[endpoint].remove(task_id)
