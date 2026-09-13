from queue import Queue
from threading import Thread

from receiver import receiver
from sender import sender


class SenderSocket:
    def __init__(self, to_receiver, to_sender):
        self.to_receiver = to_receiver
        self.to_sender = to_sender

    def sendto(self, data, _address):
        self.to_receiver.put(data)

    def recvfrom(self, _size):
        return self.to_sender.get(timeout=2), ("receiver", 4445)


class ReceiverSocket:
    def __init__(self, to_sender):
        self.to_sender = to_sender

    def sendto(self, data, _address):
        self.to_sender.put(data)


def test_ml_kem_768_establishes_the_same_32_byte_secret():
    to_receiver = Queue()
    to_sender = Queue()
    sender_socket = SenderSocket(to_receiver, to_sender)
    receiver_socket = ReceiverSocket(to_sender)
    sender_result = []
    sender_error = []

    def run_sender():
        try:
            sender_result.append(sender.wymiana_klucza(sender_socket, "ml-kem-768"))
        except Exception as exc:  # propagated below to retain the original failure
            sender_error.append(exc)

    thread = Thread(target=run_sender)
    thread.start()
    thread.join(timeout=0.1)
    if sender_error:
        raise sender_error[0]

    public_key = to_receiver.get(timeout=2)
    receiver_secret = receiver.wymiana_klucza(
        receiver_socket,
        "ml-kem-768",
        public_key,
        ("sender", 4446),
    )

    thread.join(timeout=2)
    assert not thread.is_alive()
    if sender_error:
        raise sender_error[0]

    sender_secret, generation_ns, exchange_ns, derivation_ns = sender_result[0]
    assert len(public_key) == 1184
    assert len(sender_secret) == 32
    assert sender_secret == receiver_secret
    assert generation_ns >= 0
    assert exchange_ns >= 0
    assert derivation_ns >= 0
