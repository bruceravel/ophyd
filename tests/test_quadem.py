import logging
import pytest

import epics
from ophyd import QuadEM
from .test_signal import using_fake_epics_pv


logger = logging.getLogger(__name__)


@pytest.fixture(scope='function')
@using_fake_epics_pv
def quadem():
    em = QuadEM('quadem:', name='quadem')

    ''' Beware: Ugly Hack below

        tl;dr
        Set Signal._read_pv = Signal._write_pv in order for
        set_and_wait() to succeed in calls from Device.stage()

        The raison d'etre here is a limitation of FakeEpicsPV,
        or rather a limitation of the test harness:
            Since the QuadEM is based on areadetector, it uses
            EpicsSignalWithRBV in several places. The test harness
            monkey-patches epics.PV with FakeEpicsPV, which means
            that get/put are routed to two different pvs, which in turn,
            means that set_and_wait() will never be successful for
            EpicsSignalWithRBVs... :-(
    '''
    for sig in em.stage_sigs:
        sig._read_pv = sig._write_pv

    for sig in em.image.stage_sigs:
        sig._read_pv = sig._write_pv
    em.image.enable._read_pv = em.image.enable._write_pv

    for sig in em.current1.stage_sigs:
        sig._read_pv = sig._write_pv
    em.current1.enable._read_pv = em.current1.enable._write_pv

    for sig in em.current2.stage_sigs:
        sig._read_pv = sig._write_pv
    em.current2.enable._read_pv = em.current2.enable._write_pv

    for sig in em.current3.stage_sigs:
        sig._read_pv = sig._write_pv
    em.current3.enable._read_pv = em.current3.enable._write_pv

    for sig in em.current4.stage_sigs:
        sig._read_pv = sig._write_pv
    em.current4.enable._read_pv = em.current4.enable._write_pv
    ''' End: Ugly Hack '''

    for sig in ['image'] + ['current{}'.format(j) for j in range(1, 5)]:
        cpt = getattr(em, sig)
        cpt.nd_array_port._read_pv = cpt.nd_array_port._write_pv
        cpt.port_name._read_pv.put(sig.upper())
        cpt.nd_array_port.put('NSLS2_EM')

    em.wait_for_connection()

    return em


def test_connected(quadem):
    assert quadem.connected


@using_fake_epics_pv
def test_scan_point(quadem):
    assert quadem._staged.value == 'no'

    quadem.stage()
    assert quadem._staged.value == 'yes'

    quadem.trigger()
    quadem.unstage()
    assert quadem._staged.value == 'no'


@using_fake_epics_pv
def test_reading(quadem):
    assert 'current1.mean_value' in quadem.read_attrs

    desc = quadem.describe()
    desc_keys = list(desc['quadem_current1_mean_value'].keys())
    assert (set(desc_keys) == set(['dtype', 'precision', 'shape', 'source',
                                   'units', 'lower_ctrl_limit',
                                   'upper_ctrl_limit']))

    vals = quadem.read()
    assert 'quadem_current1_mean_value' in vals
    assert (set(('value', 'timestamp')) ==
            set(vals['quadem_current1_mean_value'].keys()))
