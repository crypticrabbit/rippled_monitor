'''
Process messages from the asyncio queue.
'''
import os
import logging
import time
import asyncio

from . import process_stock_output
from . import process_validation_output
from .check_forked import fork_checker

class ResponseProcessor:
    '''
    Process remote server responses to server, ledger, and validation subscription stream messages.

    :param settings: Config file
    :param asyncio.queues.Queue message_queue: Incoming websocket messages
    :param asyncio.queues.Queue notification_queue: Outbound SMS messages
    '''
    def __init__(self, settings, message_queue, notification_queue):
        self.settings = settings
        self.table_stock = []
        self.table_validator = []
        self.forks = []
        self.ll_modes = []
        self.val_keys = []
        self.processed_validations = []
        self.message_queue = message_queue
        self.notification_queue = notification_queue
        self.time_last_output = 0
        self.time_fork_check = 0
        self.last_heartbeat = time.time()


    async def process_console_output(self):
        '''
        Call functions to print messages to the console, depending on settings.

        :param settings: Config file
        '''
        if self.settings.CONSOLE_OUT is True \
           and time.time() - self.time_last_output >= self.settings.CONSOLE_REFRESH_TIME:
            os.system('clear')
            await process_stock_output.print_table_server(self.table_stock)
            if self.table_validator:
                await process_validation_output.print_table_validation(self.table_validator)
            self.time_last_output = time.time()

    async def evaluate_forks(self):
        '''
        Call functions to check for forked servers.
        '''
        if time.time() - self.time_fork_check > self.settings.FORK_CHECK_FREQ:
            self.ll_modes, self.table_stock, self.table_validator = await fork_checker(self.settings, self.table_stock, self.table_validator, self.notification_queue)
            self.time_fork_check = time.time()

    async def sort_new_messages(self, message):
        '''
        Check if incoming messages are server, ledger, or validation subscription messages.

        :param dict message: Incoming subscription response
        '''
        # Check for server subscription messages
        if 'result' in message['data']:
            self.table_stock = \
                    await process_stock_output.update_table_server(
                        self.table_stock, self.notification_queue, message
                    )

        # Check for ledger subscription messages
        elif message['data']['type'] == 'ledgerClosed':
            self.table_stock = \
                    await process_stock_output.update_table_ledger(
                        self.table_stock, message
                    )

        # Check for validation messages
        elif message['data']['type'] == 'validationReceived':
            self.val_keys, self.table_validator, self.processed_validations = \
                    await process_validation_output.check_validations(
                        self.settings,
                        self.val_keys,
                        self.table_validator,
                        self.processed_validations,
                        message
            )

    async def generate_table_stock(self, table_stock):
        '''
        Remove the websocket connection object from the stock server table.

        :param list table_stock: Stock servers to keep track of
        '''
        table_stock_new = []
        for server in table_stock:
            dict_new = {}
            for key in server:
                if key != "ws_connection_task":
                    dict_new[key] = server[key]
            table_stock_new.append(dict_new)
        self.table_stock = table_stock_new

    async def generate_val_keys(self):
        '''
        Create a list of all potential keys for validators we are monitoring.
        '''
        val_keys = list(i.get('master_key') for i in self.table_validator) \
                + list(i.get('validation_public_key') for i in self.table_validator)

        for i in val_keys:
            if i:
                self.val_keys.append(i)
        logging.warning(f"Created initial validation key tracking list with: '{len(self.val_keys)}' items.")

    async def heartbeat_message(self):
        '''
        Send an SMS message periodically.
        '''
        if self.settings.ADMIN_HEARTBEAT \
           and time.time() - self.last_heartbeat >= self.settings.HEARTBEAT_INTERVAL:
            now = time.strftime("%m-%d %H:%M:%S", time.gmtime())
            message = "XRPL Livenet Monitor bot heartbeat. "
            message = message + str(f"LL mode: {self.ll_modes[0]}. ")
            message = message + str(f"Server time (UTC): {now}.")
            logging.warning(message)

            for admin in self.settings.ADMIN_NOTIFICATIONS:
                await self.notification_queue.put(
                    {
                        'message': message,
                        'server': admin,
                    }
                )

            self.last_heartbeat = time.time()

    async def process_messages(self, table_stock, table_validator):
        '''
        Listen for incoming messages and execute functions accordingly.

        '''
        await self.generate_table_stock(table_stock)
        self.table_validator = table_validator
        await self.generate_val_keys()

        while True:
            try:
                message = await self.message_queue.get()
                await self.sort_new_messages(message)
                await self.evaluate_forks()
                await self.process_console_output()
                await self.heartbeat_message()
            except KeyError as error :
                logging.warning(f"Error: '{error}'. Received an unexpected message: '{message}'.")
            except (asyncio.CancelledError, KeyboardInterrupt):
                logging.critical("Keyboard interrupt detected. Response processor stopped.")
                break
            except Exception as error:
                logging.critical(f"Otherwise uncaught exception in response processor: '{error}'.")
