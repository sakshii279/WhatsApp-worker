"""
servicebus_poller.py
====================
Polls Azure Service Bus queue and forwards each message to RabbitMQ.

Run:
    python servicebus_poller.py --config config.yaml
"""

import json
import time
import argparse
import logging

from azure.servicebus import ServiceBusClient
from framework import ConnectorFactory, LoggerFactory
from config_manager import ConfigManager

logging.basicConfig(level=logging.INFO)


def resolve_config(path: str) -> dict:
    ConfigManager.init(path)
    ConfigManager.load()
    return {
        "servicebus": ConfigManager.getProperty("connectors", {}).get("servicebus", {}),
        "rabbitmq"  : ConfigManager.getProperty("connectors", {}).get("rabbitmq", {}),
        "polling"   : ConfigManager.getProperty("polling", {}),
    }


def poll_once(receiver, rabbit_conn, log):
    messages = receiver.receive_messages(max_message_count=10, max_wait_time=30)
    for msg in messages:
        try:
            data = json.loads(str(msg))
            ok   = rabbit_conn.send(data)
            if ok:
                receiver.complete_message(msg)
                log.info("poller", f"Forwarded message to RabbitMQ")
            else:
                log.warn("poller", f"RabbitMQ unavailable — message left in queue")
        except Exception as exc:
            log.error("poller", f"Failed to process message", exc=exc)


def run(config_path: str):
    cfg          = resolve_config(config_path)
    sb_cfg       = cfg["servicebus"]
    interval     = cfg["polling"].get("interval_seconds", 60)
    log          = LoggerFactory.get()
    rabbit_conn  = ConnectorFactory.get("rabbitmq")

    connection_str = sb_cfg.get("connection_string", "")
    queue_name     = sb_cfg.get("queue", "whatsapp-worker-s")

    log.info("pollers", f"Starting — polling '{queue_name}' every {interval}s")

    with ServiceBusClient.from_connection_string(connection_str) as client:
        with client.get_queue_receiver(queue_name=queue_name) as receiver:
            while True:
                try:
                    poll_once(receiver, rabbit_conn, log)
                except Exception as exc:
                    log.error("poller", "Poll cycle failed", exc=exc)
                pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()
    run(args.config)