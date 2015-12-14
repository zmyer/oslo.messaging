#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import socket
import time
import traceback
import uuid

from concurrent import futures
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import importutils
from pika import exceptions as pika_exceptions
from pika import spec as pika_spec
import pika_pool
import retrying
import six


import oslo_messaging
from oslo_messaging._drivers.pika_driver import pika_exceptions as pika_drv_exc
from oslo_messaging import _utils as utils
from oslo_messaging import exceptions


LOG = logging.getLogger(__name__)

_VERSION_HEADER = "version"
_VERSION = "1.0"


class RemoteExceptionMixin(object):
    """Used for constructing dynamic exception type during deserialization of
    remote exception. It defines unified '__init__' method signature and
    exception message format
    """
    def __init__(self, module, clazz, message, trace):
        """Store serialized data
        :param module: String, module name for importing original exception
            class of serialized remote exception
        :param clazz: String, original class name of serialized remote
            exception
        :param message: String, original message of serialized remote
            exception
        :param trace: String, original trace of serialized remote exception
        """
        self.module = module
        self.clazz = clazz
        self.message = message
        self.trace = trace

        self._str_msgs = message + "\n" + "\n".join(trace)

    def __str__(self):
        return self._str_msgs


class PikaIncomingMessage(object):
    """Driver friendly adapter for received message. Extract message
    information from RabbitMQ message and provide access to it
    """

    def __init__(self, pika_engine, channel, method, properties, body, no_ack):
        """Parse RabbitMQ message

        :param pika_engine: PikaEngine, shared object with configuration and
            shared driver functionality
        :param channel: Channel, RabbitMQ channel which was used for
            this message delivery
        :param method: Method, RabbitMQ message method
        :param properties: Properties, RabbitMQ message properties
        :param body: Bytes, RabbitMQ message body
        :param no_ack: Boolean, defines should this message be acked by
            consumer or not
        """
        headers = getattr(properties, "headers", {})
        version = headers.get(_VERSION_HEADER, None)
        if not utils.version_is_compatible(version, _VERSION):
            raise pika_drv_exc.UnsupportedDriverVersion(
                "Message's version: {} is not compatible with driver version: "
                "{}".format(version, _VERSION))

        self._pika_engine = pika_engine
        self._no_ack = no_ack
        self._channel = channel
        self.delivery_tag = method.delivery_tag

        self.version = version

        self.content_type = getattr(properties, "content_type",
                                    "application/json")
        self.content_encoding = getattr(properties, "content_encoding",
                                        "utf-8")

        self.expiration_time = (
            None if properties.expiration is None else
            time.time() + float(properties.expiration) / 1000
        )

        if self.content_type != "application/json":
            raise NotImplementedError(
                "Content-type['{}'] is not valid, "
                "'application/json' only is supported.".format(
                    self.content_type
                )
            )

        message_dict = jsonutils.loads(body, encoding=self.content_encoding)

        context_dict = {}

        for key in list(message_dict.keys()):
            key = six.text_type(key)
            if key.startswith('_context_'):
                value = message_dict.pop(key)
                context_dict[key[9:]] = value
            elif key.startswith('_'):
                value = message_dict.pop(key)
                setattr(self, key[1:], value)
        self.message = message_dict
        self.ctxt = context_dict

    def acknowledge(self):
        """Ack the message. Should be called by message processing logic when
        it considered as consumed (means that we don't need redelivery of this
        message anymore)
        """
        if not self._no_ack:
            self._channel.basic_ack(delivery_tag=self.delivery_tag)

    def requeue(self):
        """Rollback the message. Should be called by message processing logic
        when it can not process the message right now and should be redelivered
        later if it is possible
        """
        if not self._no_ack:
            return self._channel.basic_nack(delivery_tag=self.delivery_tag,
                                            requeue=True)


class RpcPikaIncomingMessage(PikaIncomingMessage):
    """PikaIncomingMessage implementation for RPC messages. It expects
    extra RPC related fields in message body (msg_id and reply_q). Also 'reply'
    method added to allow consumer to send RPC reply back to the RPC client
    """

    def __init__(self, pika_engine, channel, method, properties, body, no_ack):
        """Defines default values of msg_id and reply_q fields and just call
        super.__init__ method

        :param pika_engine: PikaEngine, shared object with configuration and
            shared driver functionality
        :param channel: Channel, RabbitMQ channel which was used for
            this message delivery
        :param method: Method, RabbitMQ message method
        :param properties: Properties, RabbitMQ message properties
        :param body: Bytes, RabbitMQ message body
        :param no_ack: Boolean, defines should this message be acked by
            consumer or not
        """
        self.msg_id = None
        self.reply_q = None

        super(RpcPikaIncomingMessage, self).__init__(
            pika_engine, channel, method, properties, body, no_ack
        )

    def reply(self, reply=None, failure=None, log_failure=True):
        """Send back reply to the RPC client
        :param reply - Dictionary, reply. In case of exception should be None
        :param failure - Exception, exception, raised during processing RPC
            request. Should be None if RPC request was successfully processed
        :param log_failure, Boolean, not used in this implementation.
            It present here to be compatible with driver API
        """
        if not (self.msg_id and self.reply_q):
            return

        msg = {
            '_msg_id': self.msg_id,
        }

        if failure is not None:
            if isinstance(failure, RemoteExceptionMixin):
                failure_data = {
                    'class': failure.clazz,
                    'module': failure.module,
                    'message': failure.message,
                    'tb': failure.trace
                }
            else:
                tb = traceback.format_exception(*failure)
                failure = failure[1]

                cls_name = six.text_type(failure.__class__.__name__)
                mod_name = six.text_type(failure.__class__.__module__)

                failure_data = {
                    'class': cls_name,
                    'module': mod_name,
                    'message': six.text_type(failure),
                    'tb': tb
                }

            msg['_failure'] = failure_data

        if reply is not None:
            msg['_result'] = reply

        reply_outgoing_message = PikaOutgoingMessage(
            self._pika_engine, msg, self.ctxt, content_type=self.content_type,
            content_encoding=self.content_encoding
        )

        def on_exception(ex):
            if isinstance(ex, pika_drv_exc.ConnectionException):
                LOG.warn(str(ex))
                return True
            else:
                return False

        retrier = retrying.retry(
            stop_max_attempt_number=(
                None if self._pika_engine.rpc_reply_retry_attempts == -1
                else self._pika_engine.rpc_reply_retry_attempts
            ),
            retry_on_exception=on_exception,
            wait_fixed=self._pika_engine.rpc_reply_retry_delay * 1000,
        ) if self._pika_engine.rpc_reply_retry_attempts else None

        try:
            reply_outgoing_message.send(
                exchange=self._pika_engine.rpc_reply_exchange,
                routing_key=self.reply_q,
                confirm=True,
                mandatory=False,
                persistent=False,
                expiration_time=self.expiration_time,
                retrier=retrier
            )
            LOG.debug(
                "Message [id:'{}'] replied to '{}'.".format(
                    self.msg_id, self.reply_q
                )
            )
        except Exception:
            LOG.exception(
                "Message [id:'{}'] wasn't replied to : {}".format(
                    self.msg_id, self.reply_q
                )
            )


class RpcReplyPikaIncomingMessage(PikaIncomingMessage):
    """PikaIncomingMessage implementation for RPC reply messages. It expects
    extra RPC reply related fields in message body (result and failure).
    """
    def __init__(self, pika_engine, channel, method, properties, body, no_ack):
        """Defines default values of result and failure fields, call
        super.__init__ method and then construct Exception object if failure is
        not None

        :param pika_engine: PikaEngine, shared object with configuration and
            shared driver functionality
        :param channel: Channel, RabbitMQ channel which was used for
            this message delivery
        :param method: Method, RabbitMQ message method
        :param properties: Properties, RabbitMQ message properties
        :param body: Bytes, RabbitMQ message body
        :param no_ack: Boolean, defines should this message be acked by
            consumer or not
        """
        self.result = None
        self.failure = None

        super(RpcReplyPikaIncomingMessage, self).__init__(
            pika_engine, channel, method, properties, body, no_ack
        )

        if self.failure is not None:
            trace = self.failure.get('tb', [])
            message = self.failure.get('message', "")
            class_name = self.failure.get('class')
            module_name = self.failure.get('module')

            res_exc = None

            if module_name in pika_engine.allowed_remote_exmods:
                try:
                    module = importutils.import_module(module_name)
                    klass = getattr(module, class_name)

                    ex_type = type(
                        klass.__name__,
                        (RemoteExceptionMixin, klass),
                        {}
                    )

                    res_exc = ex_type(module_name, class_name, message, trace)
                except ImportError as e:
                    LOG.warn(
                        "Can not deserialize remote exception [module:{}, "
                        "class:{}]. {}".format(module_name, class_name, str(e))
                    )

            # if we have not processed failure yet, use RemoteError class
            if res_exc is None:
                res_exc = oslo_messaging.RemoteError(
                    class_name, message, trace
                )
            self.failure = res_exc


class PikaOutgoingMessage(object):
    """Driver friendly adapter for sending message. Construct RabbitMQ message
    and send it
    """

    def __init__(self, pika_engine, message, context,
                 content_type="application/json", content_encoding="utf-8"):
        """Parse RabbitMQ message

        :param pika_engine: PikaEngine, shared object with configuration and
            shared driver functionality
        :param message: Dictionary, user's message fields
        :param context: Dictionary, request context's fields
        :param content_type: String, content-type header, defines serialization
            mechanism
        :param content_encoding: String, defines encoding for text data
        """

        self._pika_engine = pika_engine

        self.content_type = content_type
        self.content_encoding = content_encoding

        if self.content_type != "application/json":
            raise NotImplementedError(
                "Content-type['{}'] is not valid, "
                "'application/json' only is supported.".format(
                    self.content_type
                )
            )

        self.message = message
        self.context = context

        self.unique_id = uuid.uuid4().hex

    def _prepare_message_to_send(self):
        """Combine user's message fields an system fields (_unique_id,
        context's data etc)

        :param pika_engine: PikaEngine, shared object with configuration and
            shared driver functionality
        :param message: Dictionary, user's message fields
        :param context: Dictionary, request context's fields
        :param content_type: String, content-type header, defines serialization
            mechanism
        :param content_encoding: String, defines encoding for text data
        """
        msg = self.message.copy()

        msg['_unique_id'] = self.unique_id

        for key, value in self.context.iteritems():
            key = six.text_type(key)
            msg['_context_' + key] = value
        return msg

    @staticmethod
    def _publish(pool, exchange, routing_key, body, properties, mandatory,
                 expiration_time):
        """Execute pika publish method using connection from connection pool
        Also this message catches all pika related exceptions and raise
        oslo.messaging specific exceptions

        :param pool: Pool, pika connection pool for connection choosing
        :param exchange: String, RabbitMQ exchange name for message sending
        :param routing_key: String, RabbitMQ routing key for message routing
        :param body: Bytes, RabbitMQ message payload
        :param properties: Properties, RabbitMQ message properties
        :param mandatory: Boolean, RabbitMQ publish mandatory flag (raise
            exception if it is not possible to deliver message to any queue)
        :param expiration_time: Float, expiration time in seconds
            (like time.time())
        """
        timeout = (None if expiration_time is None else
                   expiration_time - time.time())
        if timeout is not None and timeout < 0:
            raise exceptions.MessagingTimeout(
                "Timeout for current operation was expired."
            )
        try:
            with pool.acquire(timeout=timeout) as conn:
                if timeout is not None:
                    properties.expiration = str(int(timeout * 1000))
                conn.channel.publish(
                    exchange=exchange,
                    routing_key=routing_key,
                    body=body,
                    properties=properties,
                    mandatory=mandatory
                )
        except pika_exceptions.NackError as e:
            raise pika_drv_exc.MessageRejectedException(
                "Can not send message: [body: {}], properties: {}] to "
                "target [exchange: {}, routing_key: {}]. {}".format(
                    body, properties, exchange, routing_key, str(e)
                )
            )
        except pika_exceptions.UnroutableError as e:
            raise pika_drv_exc.RoutingException(
                "Can not deliver message:[body:{}, properties: {}] to any"
                "queue using target: [exchange:{}, "
                "routing_key:{}]. {}".format(
                    body, properties, exchange, routing_key, str(e)
                )
            )
        except pika_pool.Timeout as e:
            raise exceptions.MessagingTimeout(
                "Timeout for current operation was expired. {}".format(str(e))
            )
        except pika_pool.Connection.connectivity_errors as e:
            if (isinstance(e, pika_exceptions.ChannelClosed)
                    and e.args and e.args[0] == 404):
                raise pika_drv_exc.ExchangeNotFoundException(
                    "Attempt to send message to not existing exchange "
                    "detected, message: [body:{}, properties: {}], target: "
                    "[exchange:{}, routing_key:{}]. {}".format(
                        body, properties, exchange, routing_key, str(e)
                    )
                )

            raise pika_drv_exc.ConnectionException(
                "Connectivity problem detected during sending the message: "
                "[body:{}, properties: {}] to target: [exchange:{}, "
                "routing_key:{}]. {}".format(
                    body, properties, exchange, routing_key, str(e)
                )
            )
        except socket.timeout:
            raise pika_drv_exc.TimeoutConnectionException(
                "Socket timeout exceeded."
            )

    def _do_send(self, exchange, routing_key, msg_dict, confirm=True,
                 mandatory=True, persistent=False, expiration_time=None,
                 retrier=None):
        """Send prepared message with configured retrying

        :param exchange: String, RabbitMQ exchange name for message sending
        :param routing_key: String, RabbitMQ routing key for message routing
        :param msg_dict: Dictionary, message payload
        :param confirm: Boolean, enable publisher confirmation if True
        :param mandatory: Boolean, RabbitMQ publish mandatory flag (raise
            exception if it is not possible to deliver message to any queue)
        :param persistent: Boolean, send persistent message if True, works only
            for routing into durable queues
        :param expiration_time: Float, expiration time in seconds
            (like time.time())
        :param retrier: retrying.Retrier, configured retrier object for sending
            message, if None no retrying is performed
        """
        properties = pika_spec.BasicProperties(
            content_encoding=self.content_encoding,
            content_type=self.content_type,
            headers={_VERSION_HEADER: _VERSION},
            delivery_mode=2 if persistent else 1
        )

        pool = (self._pika_engine.connection_with_confirmation_pool
                if confirm else self._pika_engine.connection_pool)

        body = jsonutils.dumps(msg_dict, encoding=self.content_encoding)

        LOG.debug(
            "Sending message:[body:{}; properties: {}] to target: "
            "[exchange:{}; routing_key:{}]".format(
                body, properties, exchange, routing_key
            )
        )

        publish = (self._publish if retrier is None else
                   retrier(self._publish))

        return publish(pool, exchange, routing_key, body, properties,
                       mandatory, expiration_time)

    def send(self, exchange, routing_key='', confirm=True, mandatory=True,
             persistent=False, expiration_time=None, retrier=None):
        """Send message with configured retrying

        :param exchange: String, RabbitMQ exchange name for message sending
        :param routing_key: String, RabbitMQ routing key for message routing
        :param confirm: Boolean, enable publisher confirmation if True
        :param mandatory: Boolean, RabbitMQ publish mandatory flag (raise
            exception if it is not possible to deliver message to any queue)
        :param persistent: Boolean, send persistent message if True, works only
            for routing into durable queues
        :param expiration_time: Float, expiration time in seconds
            (like time.time())
        :param retrier: retrying.Retrier, configured retrier object for sending
            message, if None no retrying is performed
        """
        msg_dict = self._prepare_message_to_send()

        return self._do_send(exchange, routing_key, msg_dict, confirm,
                             mandatory, persistent, expiration_time, retrier)


class RpcPikaOutgoingMessage(PikaOutgoingMessage):
    """PikaOutgoingMessage implementation for RPC messages. It adds
    possibility to wait and receive RPC reply
    """
    def __init__(self, pika_engine, message, context,
                 content_type="application/json", content_encoding="utf-8"):
        super(RpcPikaOutgoingMessage, self).__init__(
            pika_engine, message, context, content_type, content_encoding
        )
        self.msg_id = None
        self.reply_q = None

    def send(self, target, reply_listener=None, expiration_time=None,
             retrier=None):
        """Send RPC message with configured retrying

        :param target: Target, oslo.messaging target which defines RPC service
        :param reply_listener: RpcReplyPikaListener, listener for waiting
            reply. If None - return immediately without reply waiting
        :param expiration_time: Float, expiration time in seconds
            (like time.time())
        :param retrier: retrying.Retrier, configured retrier object for sending
            message, if None no retrying is performed
        """

        exchange = self._pika_engine.get_rpc_exchange_name(
            target.exchange, target.topic, target.fanout, retrier is None
        )

        queue = "" if target.fanout else self._pika_engine.get_rpc_queue_name(
            target.topic, target.server, retrier is None
        )

        msg_dict = self._prepare_message_to_send()

        if reply_listener:
            msg_id = uuid.uuid4().hex
            msg_dict["_msg_id"] = msg_id
            LOG.debug('MSG_ID is %s', msg_id)

            msg_dict["_reply_q"] = reply_listener.get_reply_qname(
                expiration_time - time.time()
            )

            future = reply_listener.register_reply_waiter(msg_id=msg_id)

            self._do_send(
                exchange=exchange, routing_key=queue, msg_dict=msg_dict,
                confirm=True, mandatory=True, persistent=False,
                expiration_time=expiration_time, retrier=retrier
            )

            try:
                return future.result(expiration_time - time.time())
            except BaseException as e:
                reply_listener.unregister_reply_waiter(self.msg_id)
                if isinstance(e, futures.TimeoutError):
                    e = exceptions.MessagingTimeout()
                raise e

        else:
            self._do_send(
                exchange=exchange, routing_key=queue, msg_dict=msg_dict,
                confirm=True, mandatory=True, persistent=False,
                expiration_time=expiration_time, retrier=retrier
            )