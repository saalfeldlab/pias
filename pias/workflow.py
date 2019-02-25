from __future__ import absolute_import

import logging
import queue
import threading

from .agglomeration_model import MulticutAgglomeration
from .edge_feature_cache import EdgeFeatureCache
from .edge_labels import  EdgeLabelCache
from .random_forest import LabelsInconsistency, ModelNotTrained, RandomForestModelCache

class State(object):

    SUCCESS                       = 0
    NO_LABEL_FOR_SOME_CLASSES     = 1
    RANDOM_FOREST_TRAINING_FAILED = 2
    MC_OPTIMIZATION_FAILED        = 3



    def __init__(
            self,
            edges,
            edge_features,
            graph,
            labeled_samples,
            random_forest_kwargs
    ):
        self.edges              = edges
        self.edge_features      = edge_features
        self.graph              = graph
        self.labeled_samples    = labeled_samples
        self.random_forest      = RandomForestModelCache(labels=(0, 1), random_forest_kwargs=random_forest_kwargs)
        self.agglomeration      = MulticutAgglomeration()
        self.solution_state     = None
        self.solution           = None

    def compute(self):
        # how to get samples and labels?

        samples, labels = self.labeled_samples


        try:
            self.random_forest.train_model(samples=samples, labels=labels)
        except Exception:
            return State.RANDOM_FOREST_TRAINING_FAILED

        try:
            self.solution = self.agglomeration.optimize(self.graph, self.random_forest.predict(self.edge_features))
            return State.SUCCESS
        except Exception:
            return State.MC_OPTIMIZATION_FAILED



class Workflow(object):
    
    def __init__(
            self,
            edge_n5_container,
            edge_dataset,
            edge_feature_dataset,
            n_estimators=100,
            random_forest_kwargs=None):
        super(Workflow, self).__init__()
        self.logger = logging.getLogger('{}.{}'.format(self.__module__, type(self).__name__))
        self.logger.debug('Instantiating workflow with arguments %s', (edge_n5_container, edge_dataset, edge_feature_dataset, n_estimators, random_forest_kwargs))
        self.edge_feature_cache        = EdgeFeatureCache(edge_n5_container, edge_dataset=edge_dataset, edge_feature_dataset=edge_feature_dataset)
        self.edge_label_cache          = EdgeLabelCache()
        self.random_forest_kwargs      = dict(n_estimators=n_estimators)
        if (random_forest_kwargs is not None):
            self.random_forest_kwargs.update(random_forest_kwargs)
        # TODO do we need to lock in any place?
        self.lock                      = threading.RLock()

        self.state_update_notify        = []
        self.edge_feature_update_notify = []
        self.edge_label_update_notify   = []

        self._update_edges()
        self.latest_state            = None
        self.latest_successful_state = None


        self._is_running             = True
        self._queue_get_timeout      = 0.01
        self.update_queue            = queue.Queue()  # multiprocessing.SimpleQueue()

        self.update_worker = threading.Thread(target=self._execute_updates)
        self.update_worker.start()

    def _execute_updates(self):
        self.logger.info('Executing updates')
        while self._is_running:
            self.logger.info('Still running? %s', self._is_running)
            try:
                # why does Pycharm think that `timeout` is an unexpected argument?
                # next_task = self.update_queue.get()
                # self.logger.debug('Requesting next update task')
                next_task = self.update_queue.get(timeout=self._queue_get_timeout)
                # self.logger.debug('Received next update task %s', next_task)
                if next_task:
                    next_task()
            except queue.Empty:
                # self.logger.debug('Found empty queue')
                continue

    def request_update_state(self):
        self.update_queue.put(self._update_state)

    def _update_state(self):
        with self.lock:
            edges, edge_features, edge_index_mapping, graph = self.edge_feature_cache.get_edges_and_features()
            labeled_samples = self.edge_label_cache.get_sample_and_label_arrays(edge_features)
            state = State(
                edges = edges,
                edge_features = edge_features,
                graph = graph,
                labeled_samples = labeled_samples,
                random_forest_kwargs = self.random_forest_kwargs)
        exit_code = state.compute()
        with self.lock:
            self.latest_state = state
            if exit_code == State.SUCCESS:
                self.latest_successful_state = state
            for listener in self.state_update_notify:
                listener(exit_code, state)


    def request_update_edges(self):
        # self.update_queue.put(self._update_edges)
        return self._update_edges()

    def _update_edges(self):
        with self.lock:
            self.edge_feature_cache.update_edge_features()
            self.edge_label_cache.update_edge_index_mapping(self.edge_feature_cache.get_edges_and_features()[1])

    def request_set_edge_labels(self, edges, labels):
        # self.update_queue.put(lambda: self._set_edge_labels(edges, labels))
        return self._set_edge_labels(edges, labels)


    def _set_edge_labels(self, edges, labels):
        with self.lock:
            self.edge_label_cache.update_labels(edges, labels)


    '''
    Implement listener like this:
    def listener(exit_code, state):
        pass
    '''
    def add_solution_update_listener(self, listener):
        with self.lock:
            self.state_update_notify.append(listener)

    def get_latest_state(self):
        with self.lock:
            return self.latest_successful_state

    def stop(self):
        self._is_running = False
        self.logger.debug('Joining update worker -- self._is_running=%s', self._is_running)
        self.update_worker.join()
        self.logger.debug('Finished stopping workflow')

