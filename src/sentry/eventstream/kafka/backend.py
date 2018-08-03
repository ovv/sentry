from __future__ import absolute_import

import logging

from confluent_kafka import Producer, TopicPartition
from django.utils.functional import cached_property

from sentry import quotas
from sentry.models import Organization
from sentry.eventstream.base import EventStream
from sentry.eventstream.kafka.consumer import SynchronizedConsumer
from sentry.eventstream.kafka.protocol import parse_event_message
from sentry.tasks.post_process import post_process_group
from sentry.utils import json

logger = logging.getLogger(__name__)


# Beware! Changing this, or the message format/fields themselves requires
# consideration of all downstream consumers.
# Version 1 format: (1, '(insert|delete)', {...event json...}, {...state for post-processing...})
EVENT_PROTOCOL_VERSION = 1


class KafkaEventStream(EventStream):
    def __init__(self, publish_topic='events', producer_configuration=None, **options):
        if producer_configuration is None:
            producer_configuration = {}

        self.publish_topic = publish_topic
        self.producer_configuration = producer_configuration

    @cached_property
    def producer(self):
        return Producer(self.producer_configuration)

    def delivery_callback(self, error, message):
        if error is not None:
            logger.warning('Could not publish event (error: %s): %r', error, message)

    def publish(self, group, event, is_new, is_sample, is_regression,
                is_new_group_environment, primary_hash, skip_consume=False):
        project = event.project
        retention_days = quotas.get_event_retention(
            organization=Organization(project.organization_id)
        )

        # Polling the producer is required to ensure callbacks are fired. This
        # means that the latency between a message being delivered (or failing
        # to be delivered) and the corresponding callback being fired is
        # roughly the same as the duration of time that passes between publish
        # calls. If this ends up being too high, the publisher should be moved
        # into a background thread that can poll more frequently without
        # interfering with request handling. (This does `poll` does not act as
        # a heartbeat for the purposes of any sort of session expiration.)
        self.producer.poll(0.0)

        try:
            key = '%s:%s' % (event.project_id, event.event_id)
            value = (EVENT_PROTOCOL_VERSION, 'insert', {
                'group_id': event.group_id,
                'event_id': event.event_id,
                'organization_id': project.organization_id,
                'project_id': event.project_id,
                'message': event.message,
                'platform': event.platform,
                'datetime': event.datetime,
                'data': dict(event.data.items()),
                'primary_hash': primary_hash,
                'retention_days': retention_days,
            }, {
                'is_new': is_new,
                'is_sample': is_sample,
                'is_regression': is_regression,
                'is_new_group_environment': is_new_group_environment,
            })
            self.producer.produce(
                self.publish_topic,
                key=key.encode('utf-8'),
                value=json.dumps(value),
                on_delivery=self.delivery_callback,
            )
        except Exception as error:
            logger.warning('Could not publish event: %s', error, exc_info=True)
            raise

    def relay(self, consumer_group, commit_log_topic,
              synchronize_commit_group, commit_batch_size=100):
        consumer = SynchronizedConsumer(
            bootstrap_servers=self.producer_configuration['bootstrap.servers'],
            consumer_group=consumer_group,
            commit_log_topic=commit_log_topic,
            synchronize_commit_group=synchronize_commit_group,
        )

        consumer.subscribe(self.publish_topic)

        offsets = {}

        def commit_offsets():
            consumer.commit(offsets=[
                TopicPartition(topic, partition, offset) for (topic, partition), offset in offsets.items()
            ], asynchronous=False)

        try:
            i = 0
            while True:
                message = consumer.poll(0.1)
                if message is None:
                    continue

                error = message.error()
                if error is not None:
                    raise Exception(error)

                i = i + 1
                offsets[(message.topic(), message.partition())] = message.offset() + 1

                payload = parse_event_message(message.value())
                if payload is not None:
                    post_process_group.delay(**payload)

                if i % commit_batch_size == 0:
                    commit_offsets()
        except KeyboardInterrupt:
            pass

        logger.info('Committing offsets and closing consumer...')

        if offsets:
            commit_offsets()

        consumer.close()
