# -*- coding: utf-8 -*-
import logging
import time
from typing import List

import pandas as pd

from zvt.api.common import get_one_day_trading_minutes
from zvt.api.rules import iterate_timestamps, is_open_time, is_in_finished_timestamps, is_close_time
from zvt.charts.business import draw_account_details, draw_order_signals
from zvt.domain import SecurityType, TradingLevel, Provider
from zvt.selectors.selector import TargetSelector
from zvt.trader import TradingSignal, TradingSignalType
from zvt.trader.account import SimAccountService
from zvt.utils.time_utils import to_pd_timestamp, now_pd_timestamp

logger = logging.getLogger(__name__)


# overwrite it to custom your selector comparator
class SelectorsComparator(object):

    def __init__(self, selectors: List[TargetSelector]) -> None:
        self.selectors: List[TargetSelector] = selectors

    def make_decision(self, timestamp, trading_level: TradingLevel):
        raise NotImplementedError


# a selector comparator select the targets ordered by score and limit the targets number
class LimitSelectorsComparator(SelectorsComparator):

    def __init__(self, selectors: List[TargetSelector], limit=10) -> None:
        super().__init__(selectors)
        self.limit = limit

    def make_decision(self, timestamp, trading_level: TradingLevel):
        df_result = pd.DataFrame()
        for selector in self.selectors:
            if selector.level == trading_level:
                logger.info('{} selector:{} make_decision'.format(trading_level.value, selector))

                df = selector.get_targets(timestamp)
                if not df.empty:
                    df = df.sort_values(by=['score', 'security_id'])
                    if len(df.index) > self.limit:
                        df = df.iloc[list(range(self.limit)), :]
                df_result = df_result.append(df)
        return df_result


# the data structure for storing level:targets map,you should handle the targets of the level before overwrite it
class TargetsSlot(object):

    def __init__(self) -> None:
        self.level_map_targets = {}

    def input_targets(self, level: TradingLevel, targets: List[str]):
        logger.info('level:{},old targets:{},new targets:{}'.format(level,
                                                                    self.get_targets(level), targets))
        self.level_map_targets[level.value] = targets

    def get_targets(self, level: TradingLevel):
        return self.level_map_targets.get(level.value)


class Trader(object):
    logger = logging.getLogger(__name__)

    def __init__(self, security_list=None, security_type=SecurityType.stock, exchanges=['sh', 'sz'], codes=None,
                 start_timestamp=None,
                 end_timestamp=None,
                 provider=Provider.JOINQUANT,
                 trading_level=TradingLevel.LEVEL_1DAY,
                 trader_name=None,
                 real_time=False,
                 kdata_use_begin_time=False) -> None:
        """

        :param security_list:
        :type security_list:
        :param security_type:
        :type security_type:
        :param exchanges:
        :type exchanges:
        :param codes:
        :type codes:
        :param start_timestamp:
        :type start_timestamp:
        :param end_timestamp:
        :type end_timestamp:
        :param provider:
        :type provider:
        :param trading_level:
        :type trading_level:
        :param trader_name:
        :type trader_name:
        :param real_time:
        :type real_time:
        :param kdata_use_begin_time: true means the interval [timestamp,timestamp+level),false means [timestamp-level,timestamp)
        :type kdata_use_begin_time: bool

        """
        if trader_name:
            self.trader_name = trader_name
        else:
            self.trader_name = type(self).__name__.lower()

        self.trading_signal_listeners = []
        self.state_listeners = []

        self.selectors: List[TargetSelector] = None

        self.security_list = security_list
        self.security_type = security_type
        self.exchanges = exchanges
        self.codes = codes

        self.provider = provider
        # make sure the min level selector correspond to the provider and level
        self.trading_level = trading_level
        self.real_time = real_time

        if start_timestamp and end_timestamp:
            self.start_timestamp = to_pd_timestamp(start_timestamp)
            self.end_timestamp = to_pd_timestamp(end_timestamp)
        else:
            assert False

        if real_time:
            logger.info(
                'real_time mode, end_timestamp should be future,you could set it big enough for running forever')
            assert self.end_timestamp >= now_pd_timestamp()

        self.kdata_use_begin_time = kdata_use_begin_time

        self.account_service = SimAccountService(trader_name=self.trader_name,
                                                 timestamp=self.start_timestamp,
                                                 provider=self.provider,
                                                 level=self.trading_level)

        self.add_trading_signal_listener(self.account_service)

        self.init_selectors(security_list=security_list, security_type=self.security_type, exchanges=self.exchanges,
                            codes=self.codes, start_timestamp=self.start_timestamp, end_timestamp=self.end_timestamp)

        self.selectors_comparator = LimitSelectorsComparator(self.selectors)

        self.trading_level_asc = list(set([TradingLevel(selector.level) for selector in self.selectors]))
        self.trading_level_asc.sort()

        self.trading_level_desc = list(self.trading_level_asc)
        self.trading_level_desc.reverse()

        self.targets_slot: TargetsSlot = TargetsSlot()

    def init_selectors(self, security_list, security_type, exchanges, codes, start_timestamp, end_timestamp):
        """
        implement this to init selectors

        """
        raise NotImplementedError

    def add_trading_signal_listener(self, listener):
        if listener not in self.trading_signal_listeners:
            self.trading_signal_listeners.append(listener)

    def remove_trading_signal_listener(self, listener):
        if listener in self.trading_signal_listeners:
            self.trading_signal_listeners.remove(listener)

    def handle_targets_slot(self, timestamp):
        """
        this function would be called in every cycle, you could overwrite it for your custom algorithm to select the
        targets of different levels

        the default implementation is selecting the targets in all levels

        :param timestamp:
        :type timestamp:
        """
        selected = None
        for level in self.trading_level_desc:
            targets = self.targets_slot.get_targets(level=level)
            if not targets:
                targets = set()

            if not selected:
                selected = targets
            else:
                selected = selected & targets

        if selected:
            self.logger.info('timestamp:{},selected:{}'.format(timestamp, selected))

        self.send_trading_signals(timestamp=timestamp, selected=selected)

    def send_trading_signals(self, timestamp, selected):
        # current position
        account = self.account_service.latest_account
        current_holdings = [position['security_id'] for position in account['positions'] if
                            position['available_long'] > 0]

        if selected:
            # just long the security not in the positions
            longed = selected - set(current_holdings)
            if longed:
                position_pct = 1.0 / len(longed)
                order_money = account['cash'] * position_pct

                for security_id in longed:
                    trading_signal = TradingSignal(security_id=security_id,
                                                   the_timestamp=timestamp,
                                                   trading_signal_type=TradingSignalType.trading_signal_open_long,
                                                   trading_level=self.trading_level,
                                                   order_money=order_money)
                    for listener in self.trading_signal_listeners:
                        listener.on_trading_signal(trading_signal)

        # just short the security not in the selected but in current_holdings
        if selected:
            shorted = set(current_holdings) - selected
        else:
            shorted = set(current_holdings)

        for security_id in shorted:
            trading_signal = TradingSignal(security_id=security_id,
                                           the_timestamp=timestamp,
                                           trading_signal_type=TradingSignalType.trading_signal_close_long,
                                           position_pct=1.0,
                                           trading_level=self.trading_level)
            for listener in self.trading_signal_listeners:
                listener.on_trading_signal(trading_signal)

    def on_finish(self):
        draw_account_details(trader_name=self.trader_name)
        draw_order_signals(trader_name=self.trader_name)

    def run(self):
        # iterate timestamp of the min level,e.g,9:30,9:35,9.40...for 5min level
        # timestamp represents the timestamp in kdata
        handled_timestamp = None
        for timestamp in iterate_timestamps(security_type=self.security_type, exchange=self.exchanges[0],
                                            start_timestamp=self.start_timestamp, end_timestamp=self.end_timestamp,
                                            level=self.trading_level):
            if self.real_time and handled_timestamp:
                # all selector move on to handle the coming data
                if self.kdata_use_begin_time:
                    touching_timestamp = handled_timestamp + pd.Timedelta(seconds=self.trading_level.to_second())
                else:
                    touching_timestamp = handled_timestamp

                waiting_seconds, _ = self.trading_level.count_from_timestamp(touching_timestamp,
                                                                             one_day_trading_minutes=get_one_day_trading_minutes(
                                                                                 security_type=self.security_type))
                if waiting_seconds and (waiting_seconds > 10):
                    t = waiting_seconds / 2
                    self.logger.info(
                        'level:{},handled_timestamp:{},touching_timestamp:{},current_time:{},next_ok_time:{},just sleep:{} seconds'.format(
                            self.trading_level.value,
                            handled_timestamp,
                            touching_timestamp,
                            now_pd_timestamp(),
                            touching_timestamp + pd.Timedelta(
                                seconds=self.trading_level.to_second()),
                            t))

                    time.sleep(t)

                    for selector in self.selectors:
                        if (is_in_finished_timestamps(security_type=self.security_type, exchange=self.exchanges[0],
                                                      timestamp=timestamp, level=selector.level)):
                            if self.kdata_use_begin_time:
                                to_touching_timestamp = timestamp + pd.Timedelta(
                                    seconds=selector.level.to_second())
                            else:
                                to_touching_timestamp = timestamp

                            selector.move_on(timestamp, to_touching_timestamp)

            # on_trading_open to setup the account
            if self.trading_level == TradingLevel.LEVEL_1DAY or (
                    self.trading_level != TradingLevel.LEVEL_1DAY and is_open_time(security_type=self.security_type,
                                                                                   exchange=self.exchanges[0],
                                                                                   timestamp=timestamp)):
                self.account_service.on_trading_open(timestamp)

            # the time always move on by min level step and we could check all level targets in the slot
            self.handle_targets_slot(timestamp=timestamp)

            for level in self.trading_level_asc:
                # in every cycle, all level selector do its job in its time
                if (is_in_finished_timestamps(security_type=self.security_type, exchange=self.exchanges[0],
                                              timestamp=timestamp, level=level)):
                    df = self.selectors_comparator.make_decision(timestamp=timestamp,
                                                                 trading_level=level)
                    if not df.empty:
                        selected = set(df['security_id'].to_list())
                    else:
                        selected = {}

                    self.targets_slot.input_targets(level, selected)

            handled_timestamp = timestamp

            # on_trading_close to calculate date account
            if self.trading_level == TradingLevel.LEVEL_1DAY or (
                    self.trading_level != TradingLevel.LEVEL_1DAY and is_close_time(security_type=self.security_type,
                                                                                    exchange=self.exchanges[0],
                                                                                    timestamp=timestamp)):
                self.account_service.on_trading_close(timestamp)

        self.on_finish()