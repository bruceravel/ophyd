#!/usr/bin/env python2.7
'''
A simple test for :class:`EpicsMotor`
'''

import time

import config
from ophyd.controls import EpicsMotor


def test():
    def callback(sub_type=None, timestamp=None, value=None, **kwargs):
        logger.info('[callback] [%s] (type=%s) value=%s' % (timestamp, sub_type, value))

    def done_moving(**kwargs):
        logger.info('Done moving %s' % (kwargs, ))

    loggers = ('ophyd.controls.signal',
               'ophyd.controls.positioner',
               'ophyd.session',
               )

    config.setup_loggers(loggers)
    logger = config.logger

    motor_record = config.motor_recs[0]

    m1 = EpicsMotor(motor_record)
    # m2 = EpicsMotor('MLL:bad_record')
    m1.subscribe(callback, event_type=m1.SUB_DONE)

    m1.subscribe(callback, event_type=m1.SUB_READBACK)
    # print(m1.user_readback.read())
    # print(m1.read())

    logger.info('---- test #1 ----')
    logger.info('--> move to 1')
    m1.move(1)
    logger.info('--> move to 0')
    m1.move(0)

    logger.info('---- test #2 ----')
    logger.info('--> move to 1')
    m1.move(1, wait=False)
    time.sleep(0.2)
    logger.info('--> stop')
    m1.stop()
    logger.info('--> sleep')
    time.sleep(1)
    logger.info('--> move to 0')
    m1.move(0, wait=False, moved_cb=done_moving)
    time.sleep(2)

    logger.debug('limits are: {}'.format(m1.limits))
    low_lim, high_lim = m1.low_limit, m1.high_limit
    try:
        m1.move(high_lim + 1)
    except ValueError as ex:
        logger.debug('Failed move, as expected (%s)' % ex)
    else:
        raise ValueError('Move should have failed')

    try:
        m1.move(low_lim - 1)
    except ValueError as ex:
        logger.debug('Failed move, as expected (%s)' % ex)
    else:
        raise ValueError('Move should have failed')

    try:
        m1.check_value(low_lim - 1)
    except ValueError as ex:
        logger.debug('Failed check_value, as expected (%s)' % ex)
    else:
        raise ValueError('check_value should have failed')

    logger.info('--> move to 0')
    stat = m1.move(2, wait=False)

    while not stat.done:
        logger.info('--> moving... %s error=%s' % (stat, stat.error))
        time.sleep(0.1)

    logger.debug(m1.get())
    logger.debug(m1.request_ts)
    logger.debug(m1.timestamp)
    logger.debug(m1.pvname)
    logger.debug(m1.request_pvname)

    prec = m1.precision
    fmt = '%%.%df' % prec
    print(fmt % m1.position)


if __name__ == '__main__':
    test()
