import pickle
from svod_rcgn.tools.print import print_fun

import zmq

DEFAULT_LISTENER_PORT = 43210


def listener_args(args):
    return SVODListener(
        port=args.listener_port,
    )


class SVODListener:
    def __init__(self, port=DEFAULT_LISTENER_PORT):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind('tcp://127.0.0.1:%d' % port)

    def listen(self):
        while True:
            try:
                command, data = pickle.loads(self.socket.recv())
                # test, remove
                if command == "error":
                    raise ValueError("test error")
                return command, data
            except Exception as e:
                print_fun("listener exception: %s" % e)
                self.result(e)

    def result(self, err=None):
        self.socket.send(pickle.dumps(err))


def add_listener_args(parser):
    parser.add_argument(
        '--listener_port',
        type=int,
        default=DEFAULT_LISTENER_PORT,
        help='Listener port',
    )
