from __future__ import print_function

import contextlib
import logging
import os
import shutil
import tempfile
import threading
import unittest

import numpy as np
import z5py
import zmq

from pias import SolverServer
from pias import zmq_util
from pias.solver_server import _NO_SOLUTION_AVAILABLE, _SET_EDGE_REQ_EDGE_LIST, _SET_EDGE_REP_SUCCESS, \
    _SET_EDGE_REP_DO_NOT_UNDERSTAND, _SET_EDGE_REP_EXCEPTION
from pias.threading import CountDownLatch


@contextlib.contextmanager
def _tempdir():
    """A context manager for creating and then deleting a temporary directory."""
    tmpdir = tempfile.mkdtemp()
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir)

def _mk_dummy_edge_data(
        container,
        edge_dataset=SolverServer.default_edge_dataset(),
        edge_feature_dataset=SolverServer.default_edge_feature_dataset()):

    edges = np.array(
        [[0, 1],
         [1, 2],
         [0, 2],
         [1, 3],
         [2, 3]],
        dtype=np.uint64)

    features = np.array(
        [[0.5, 1.0, 0.5],
         [0.7, 0.9, 0.8],
         [0.3, 0.9, 0.2],
         [0.5, 0.2, 0.6],
         [0.4, 0.1, 0.3]])

    labels = (0, 0, 0, 1, 1)

    f = z5py.File(container, 'w', use_zarr_format=False)
    f.create_dataset(edge_dataset, data=edges, dtype=np.uint64)
    f.create_dataset(edge_feature_dataset, data=features)

    return edges, features, labels


class TestSolverServerPing(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(TestSolverServerPing, self).__init__(*args, **kwargs)
        self.logger = logging.getLogger('{}.{}'.format(self.__module__, type(self).__name__))

    def test(self):

        with _tempdir() as tmpdir:
            address_base = 'inproc://address'
            container    = os.path.join(tmpdir, 'edge-group')
            _mk_dummy_edge_data(container)
            self.logger.debug('Starting solver server')
            server = SolverServer(
                address_base=address_base,
                edge_n5_container=container)
            self.logger.debug('Started solver server')

            ping_socket = server.context.socket(zmq.REQ)
            ping_socket.setsockopt(zmq.SNDTIMEO, 30)
            ping_socket.setsockopt(zmq.RCVTIMEO, 30)
            ping_socket.connect(server.get_ping_address())

            # test ping three times
            for i in range(3):
                self.logger.debug('sending ping')
                ping_socket.send_string('')
                self.logger.debug('waiting for pong')
                ping_response = ping_socket.recv_string()
                self.logger.debug('pong is `%s\'', ping_response)
                self.assertEqual('', ping_response)

            server.shutdown()

class TestSolverCurrentSolution(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(TestSolverCurrentSolution, self).__init__(*args, **kwargs)
        self.logger = logging.getLogger('{}.{}'.format(self.__module__, type(self).__name__))

    def test(self):

        with _tempdir() as tmpdir:
            address_base = 'inproc://address'
            container    = os.path.join(tmpdir, 'edge-group')
            _mk_dummy_edge_data(container)
            server = SolverServer(
                address_base=address_base,
                edge_n5_container=container)

            try:
                current_solution_socket = server.context.socket(zmq.REQ)
                current_solution_socket.setsockopt(zmq.SNDTIMEO, 30)
                current_solution_socket.setsockopt(zmq.RCVTIMEO, 30)
                current_solution_socket.connect(server.get_current_solution_address())
                current_solution_socket.send_string('')
                solution   = zmq_util.recv_int(current_solution_socket)
                extra_info = current_solution_socket.recv_string()
                self.logger.debug('extra info `%s\'', extra_info)
                self.assertEqual(_NO_SOLUTION_AVAILABLE, solution)
                self.assertEqual('', extra_info)
            finally:
                server.shutdown()

class TestSolverSetEdgeLabels(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(TestSolverSetEdgeLabels, self).__init__(*args, **kwargs)
        self.logger = logging.getLogger('{}.{}'.format(self.__module__, type(self).__name__))

    def test(self):

        with _tempdir() as tmpdir:
            address_base = 'inproc://address'
            container    = os.path.join(tmpdir, 'edge-group')
            edges, _, labels = _mk_dummy_edge_data(container)
            server = SolverServer(
                address_base=address_base,
                edge_n5_container=container)

            edge_label_socket = server.context.socket(zmq.REQ)
            edge_label_socket.setsockopt(zmq.SNDTIMEO, 30)
            edge_label_socket.setsockopt(zmq.RCVTIMEO, 30)
            edge_label_socket.connect(server.get_edge_labels_address())
            edge       = (edges[0, 0].item(), edges[0, 1].item(), labels[0])
            zmq_util.send_more_int(edge_label_socket, _SET_EDGE_REQ_EDGE_LIST)
            edge_label_socket.send(zmq_util._edges_as_bytes((edge,)))
            response_code = zmq_util.recv_int(edge_label_socket)
            self.logger.debug('Received response code %s', response_code)
            self.assertEqual(_SET_EDGE_REP_SUCCESS , response_code)
            num_edges = zmq_util.recv_int(edge_label_socket)
            self.assertEqual(1, num_edges)

            edges_as_tuples = tuple((e[0].item(), e[1].item(), l) for e, l in zip(edges, labels))
            zmq_util.send_more_int(edge_label_socket, _SET_EDGE_REQ_EDGE_LIST)
            edge_label_socket.send(zmq_util._edges_as_bytes(edges_as_tuples))
            response_code = zmq_util.recv_int(edge_label_socket)
            self.assertEqual(_SET_EDGE_REP_SUCCESS, response_code)
            num_edges = zmq_util.recv_int(edge_label_socket)
            self.assertEqual(len(labels), num_edges)

            zmq_util.send_ints_multipart(edge_label_socket, -1, 0)
            response_code, message_type = zmq_util.recv_ints_multipart(edge_label_socket)
            self.assertEqual(_SET_EDGE_REP_DO_NOT_UNDERSTAND, response_code)
            self.assertEqual(-1, message_type)

            zmq_util.send_more_int(edge_label_socket, _SET_EDGE_REQ_EDGE_LIST)
            edge_label_socket.send(bytearray(8))
            response_code = zmq_util.recv_int(edge_label_socket)
            self.assertEqual(_SET_EDGE_REP_EXCEPTION, response_code)
            exception = edge_label_socket.recv_string()
            self.logger.debug('Expected exception is: `%s\'', exception)


            server.shutdown()

class TestRequestUpdateSolution(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(TestRequestUpdateSolution, self).__init__(*args, **kwargs)
        self.logger = logging.getLogger('{}.{}'.format(self.__module__, type(self).__name__))

    def test(self):

        with _tempdir() as tmpdir:
            address_base = 'inproc://address'
            container    = os.path.join(tmpdir, 'edge-group')
            edges, features, labels = _mk_dummy_edge_data(container)
            server = SolverServer(
                address_base=address_base,
                edge_n5_container=container)

            expected_solution_infos = ((0, 2), (1, 2), (2, 0))
            solution_infos = []
            new_solution_address = server.get_new_solution_address()
            self.logger.debug('Connecting to %s for new solutions', new_solution_address)
            new_solution_listener = server.context.socket(zmq.SUB)
            new_solution_listener.setsockopt(zmq.RCVTIMEO, 30)
            new_solution_listener.setsockopt(zmq.SUBSCRIBE, b'')
            new_solution_listener.connect(new_solution_address)

            try:

                n_examples = len(expected_solution_infos)
                latch      = CountDownLatch(n_examples)

                def listen_for_new_solution():
                    while latch.get_count() > 0:
                        try:
                            new_solution_info = zmq_util.recv_ints(new_solution_listener)
                            self.logger.debug('Received notification about new solution %s', new_solution_info)
                            self.assertEqual(expected_solution_infos[len(solution_infos)], new_solution_info, 'Failed for example %d' % (n_examples - latch.get_count()))
                            solution_infos.append(new_solution_info)
                            latch.count_down()
                        except zmq.error.Again as e:
                            self.logger.debug('Ignoring exception of type %s: %s', type(e), e)

                new_solution_listener_thread = threading.Thread(target=listen_for_new_solution, daemon=True)
                new_solution_listener_thread.start()

                request_solution_update = server.context.socket(zmq.REQ)
                request_solution_update.setsockopt(zmq.SNDTIMEO, 30)
                request_solution_update.setsockopt(zmq.RCVTIMEO, 30)
                request_solution_update.connect(server.get_solution_update_request_address())

                request_solution_update.send_string('')
                response = zmq_util.recv_ints_multipart(request_solution_update)
                self.logger.debug('First update request response: %s', response)
                self.assertEqual(2, len(response))
                self.assertEqual(0, response[0]) # successfully submitted
                self.assertEqual(0, response[1]) # state id
                latch.wait_until_value(value=n_examples-1, timeout=0.01)

                request_solution_update.send_string('')
                response = zmq_util.recv_ints_multipart(request_solution_update)
                self.logger.debug('Second update request response: %s', response)
                self.assertEqual(2, len(response))
                self.assertEqual(0, response[0]) # successfully submitted
                self.assertEqual(1, response[1]) # state id
                latch.wait_until_value(value=n_examples-2, timeout=0.01)

                # send actual samples for a valid solution
                edge_label_socket = server.context.socket(zmq.REQ)
                edge_label_socket.setsockopt(zmq.SNDTIMEO, 30)
                edge_label_socket.setsockopt(zmq.RCVTIMEO, 30)
                edge_label_socket.connect(server.get_edge_labels_address())

                samples = tuple((edges[e, 0].item(), edges[e, 1].item(), labels[e]) for e in (0, -1))
                self.logger.debug('Sending samples %s', samples)

                zmq_util.send_more_int(edge_label_socket, _SET_EDGE_REQ_EDGE_LIST)
                edge_label_socket.send(zmq_util._edges_as_bytes(samples))
                response_code = zmq_util.recv_int(edge_label_socket)
                self.logger.debug('Received response code %s', response_code)
                self.assertEqual(_SET_EDGE_REP_SUCCESS , response_code)
                num_edges = zmq_util.recv_int(edge_label_socket)
                self.assertEqual(2, num_edges)

                request_solution_update.send_string('')
                response = zmq_util.recv_ints_multipart(request_solution_update)
                self.logger.debug('Second update request response: %s', response)
                self.assertEqual(2, len(response))
                self.assertEqual(0, response[0]) # successfully submitted
                self.assertEqual(2, response[1]) # state id

                self.logger.debug('Waiting for countdown latch')
                latch_timeout = 2
                latch.wait_for_countdown(timeout=latch_timeout)
                latch_remaining = latch.get_count()
                self.logger.debug('%d count downs remaining', latch_remaining)
                self.assertEqual(0, latch_remaining, 'Did not receive all solution update notifications within %f seconds. (%d remaining)' % (latch_timeout, latch_remaining))
                self.logger.debug('Count down latch reached zero')

                new_solution_listener_thread.join()

                get_solution_socket = server.context.socket(zmq.REQ)
                get_solution_socket.connect(server.get_current_solution_address())

                get_solution_socket.send(b'')
                solution_exit_code = zmq_util.recv_int(get_solution_socket)
                self.logger.debug('solution exit code %d', solution_exit_code)
                self.assertEqual(0, solution_exit_code)
                solution = get_solution_socket.recv()
                solution_ndarray = zmq_util._bytes_as_ndarray(solution, dtype=np.uint64)
                self.logger.debug('solution as ndarray: %s', solution_ndarray)
                self.assertEqual(4, solution_ndarray.size)
                self.assertEqual((4,), solution_ndarray.shape)
                labels_first_three = np.unique(solution_ndarray[:3])
                self.logger.debug('unique labels for first three entries: %s', labels_first_three)
                self.assertEqual(1, labels_first_three.size)
                self.assertNotEqual(labels_first_three[0], solution_ndarray[3])

            finally:
                server.shutdown()