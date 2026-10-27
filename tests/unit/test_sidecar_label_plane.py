"""The archive sidecar resolves to the same plane as the ring (task 14b).

Audit G6 exists to keep two paths together: the ring's anchor, and the
per-chunk sidecar the writer emits.  Task 13a moved the ring onto the
verified registration and left the writer applying the judge's verdict to
radiod's host-stamped pair — so on a station whose judge best-bench is a
host-plane bench the two could differ by the host's error.  A raw-IQ
consumer reading only the sidecar would see the other plane.

mjh's ruling: wherever a label-plane native anchor is in force, that
anchor is the ONLY source of RTP→UTC for every published surface.  Here
that means the sidecar's pair IS the anchor's plane restated at radiod's
snap counter, `timing.offset_ns` is zero because the pair already carries
the plane, and the judge's reading survives as a witness.
"""

import pytest

from hf_timestd.core import buffer_timing as btm
from hf_timestd.core.binary_archive_writer import (
    BinaryArchiveConfig,
    BinaryArchiveWriter,
)
from hf_timestd.core.native_anchor import LabelAnchor, NativeAnchor
from hf_timestd.core.offset_judge import OffsetVerdict

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)
RTP_SNAP = 1000


class StubJudge:
    def __init__(self, verdict=None):
        self.verdict = verdict
        self.registered = []

    def register_radiod_pair(self, key, gps_ns, snap, rate):
        self.registered.append((key, int(gps_ns), int(snap), int(rate)))

    def offset_for(self, key, rtp):
        return self.verdict


def label(
    utc_ref=WALL0,
    rtp_ref=RTP_SNAP,
    sample_rate=SR,
    tier="T3",
    sigma_ns=1.0e6,
    epoch="ep-1",
):
    return LabelAnchor(
        anchor=NativeAnchor(
            anchor_rtp=rtp_ref,
            anchor_utc_ns=int(round(utc_ref * 1e9)),
            sample_rate_hz=sample_rate,
            chain_delay_ns=0,
            captured_at_utc_ns=int(round(utc_ref * 1e9)),
            captured_via_tier=tier,
        ),
        epoch_id=epoch,
        tier=tier,
        sigma_ns=sigma_ns,
    )


def make_writer(tmp_path, judge=None, provider=None, sample_rate=SR):
    cfg = BinaryArchiveConfig(
        channel_name="TEST_10_MHz",
        frequency_hz=10e6,
        sample_rate=sample_rate,
        output_dir=tmp_path / "raw",
        compression="none",
        file_duration_sec=60,
    )
    w = BinaryArchiveWriter(cfg, offset_judge=judge, source_key=KEY)
    if provider is not None:
        w.set_label_anchor_provider(provider)
    return w


def host_stamped_pair(unix_s):
    """radiod's GPS_TIME for a host clock reading `unix_s`."""
    return btm.unix_ns_to_gps_time_ns(int(round(unix_s * 1e9)))


def resolve(metadata, rtp, sample_rate=SR):
    md = dict(metadata)
    md["start_rtp_timestamp"] = rtp
    return btm.resolve_buffer_timing(md, sample_rate=sample_rate).sample0_utc


# ── the correction is the plane offset, not the verdict ──────────────


def test_the_correction_carries_radiods_plane_onto_the_anchors(tmp_path):
    """radiod's pair is 150 ms fast; the registration is right.  The
    correction must be −150 ms whatever the judge happens to say."""
    la = label()
    verdict = OffsetVerdict(+42e6, 1e6, "T4", 1.0, 1, False)  # nonsense
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: la)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0 + 0.150), rtp_timesnap=RTP_SNAP
        )
        assert w._label_correction_s(verdict) == pytest.approx(-0.150, abs=1e-6)
    finally:
        w.close()


def test_without_an_anchor_the_correction_is_the_judges_offset(tmp_path):
    verdict = OffsetVerdict(+42e6, 1e6, "T4", 1.0, 1, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: None)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0), rtp_timesnap=RTP_SNAP
        )
        assert w._label_correction_s(verdict) == pytest.approx(0.042, abs=1e-9)
        assert w._label_correction_s(None) == 0.0
    finally:
        w.close()


def test_no_anchor_before_timing_lock(tmp_path):
    """Nothing to restate: the anchor's plane is expressed at radiod's
    snap, and before lock there is no snap."""
    w = make_writer(tmp_path, judge=StubJudge(), provider=lambda: label())
    try:
        assert w._label_anchor() is None
        assert w._label_correction_s(None) == 0.0
    finally:
        w.close()


def test_a_foreign_counter_domain_is_refused(tmp_path):
    w = make_writer(
        tmp_path, judge=StubJudge(), provider=lambda: label(sample_rate=96000)
    )
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0), rtp_timesnap=RTP_SNAP
        )
        assert w._label_anchor() is None
    finally:
        w.close()


def test_a_raising_provider_never_disturbs_the_writer(tmp_path):
    def boom():
        raise RuntimeError("nope")

    verdict = OffsetVerdict(+42e6, 1e6, "T4", 1.0, 1, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=boom)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0), rtp_timesnap=RTP_SNAP
        )
        assert w._label_anchor() is None
        assert w._label_correction_s(verdict) == pytest.approx(0.042, abs=1e-9)
    finally:
        w.close()


# ── the sidecar: one plane, resolved through the real resolver ───────


def _sidecar(w, verdict, chunk_boundary=WALL0):
    """The sidecar fields task 14b writes, built the way the writer does."""
    la = w._label_anchor()
    block = w._chunk_timing_block(
        verdict,
        chunk_boundary_utc_ns=int(round(chunk_boundary * 1e9)),
        label=la,
    )
    pair = w._label_pair(la)
    return {
        "gps_time_ns": pair[0] if pair else w._gps_time_ns_raw,
        "rtp_timesnap": pair[1] if pair else w._rtp_timesnap,
        "timing": block,
    }


def test_sidecar_ring_and_registration_agree_at_the_same_rtp(tmp_path):
    """THE task-14b assertion.  All three resolvers, one plane.

    radiod's pair is 150 ms fast and the judge is offering a wrong
    correction; the registration says sample RTP_SNAP was at WALL0.
    """
    la = label()
    verdict = OffsetVerdict(+42e6, 1e6, "T4", 1.0, 1, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: la)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0 + 0.150), rtp_timesnap=RTP_SNAP
        )
        md = _sidecar(w, verdict)
        # The ring's own pair (StreamRecorderV2._anchor_ring_from_label_plane).
        ring_md = {
            "gps_time_ns": btm.unix_ns_to_gps_time_ns(int(la.anchor.anchor_utc_ns)),
            "rtp_timesnap": int(la.anchor.anchor_rtp),
        }
        for rtp in (RTP_SNAP, RTP_SNAP + 30 * SR, RTP_SNAP - 5 * SR):
            registration_utc = la.utc_ns_at(rtp) / 1e9
            sidecar_utc = resolve(md, rtp)
            ring_utc = resolve(ring_md, rtp)
            assert sidecar_utc == pytest.approx(registration_utc, abs=1e-6)
            assert ring_utc == pytest.approx(registration_utc, abs=1e-6)
        # and not the host-stamped plane
        assert resolve(md, RTP_SNAP) == pytest.approx(WALL0, abs=1e-6)
    finally:
        w.close()


def test_the_reapplied_offset_is_zero_so_nothing_is_counted_twice(tmp_path):
    """`resolve_buffer_timing` ADDS `timing.offset_ns` to the pair.  With
    the anchor's plane already in the pair, a non-zero offset there would
    apply the correction twice — the exact double-count the sign trace
    has to rule out."""
    la = label()
    verdict = OffsetVerdict(-150e6, 1e6, "T3", 1.0, 1, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: la)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0 + 0.150), rtp_timesnap=RTP_SNAP
        )
        md = _sidecar(w, verdict)
        assert md["timing"]["offset_ns"] == 0.0
        bt = btm.resolve_buffer_timing(
            dict(md, start_rtp_timestamp=RTP_SNAP), sample_rate=SR
        )
        assert bt.offset_applied_ns == 0.0
        assert bt.sample0_utc == pytest.approx(WALL0, abs=1e-6)
        # ...and NOT WALL0 − 0.150, which is what a re-applied judge
        # offset on top of the anchored pair would give.
        assert abs(bt.sample0_utc - (WALL0 - 0.150)) > 0.149
    finally:
        w.close()


def test_the_block_records_the_plane_and_keeps_the_judge_as_witness(tmp_path):
    la = label()
    verdict = OffsetVerdict(-150e6, 25e6, "T4", 1.0, 7, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: la)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0 + 0.150), rtp_timesnap=RTP_SNAP
        )
        block = _sidecar(w, verdict)["timing"]
        assert block["plane_source"] == "t3_registration"
        assert block["judge_tier"] == "T3"  # the PLANE's tier
        assert block["offset_sigma_ns"] == pytest.approx(1.0e6)
        assert block["anchor_rtp"] == RTP_SNAP
        assert block["anchor_utc_ns"] == int(WALL0 * 1e9)
        assert block["anchor_epoch_id"] == "ep-1"
        # the witness record: what the judge measured, never erased
        assert block["judge_offset_ns"] == pytest.approx(-150e6)
        assert block["judge_offset_sigma_ns"] == pytest.approx(25e6)
        assert block["judge_witness_tier"] == "T4"
        assert block["segment_id"] == 7
        # radiod's raw pair stays in the record too
        assert block["radiod_rtp_timesnap"] == RTP_SNAP
        assert block["radiod_gps_time_ns"] == host_stamped_pair(WALL0 + 0.150)
    finally:
        w.close()


def test_a_t6_anchor_names_itself(tmp_path):
    la = label(tier="T6", sigma_ns=50_000.0)
    verdict = OffsetVerdict(0.0, 1e6, "T6", 1.0, 1, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: la)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0), rtp_timesnap=RTP_SNAP
        )
        block = _sidecar(w, verdict)["timing"]
        assert block["plane_source"] == "t6_native"
        assert block["judge_tier"] == "T6"
        assert block["offset_sigma_ns"] == pytest.approx(50_000.0)
    finally:
        w.close()


def test_without_an_anchor_the_sidecar_is_the_judged_pair(tmp_path):
    """Byte-identical pre-task-14 behaviour."""
    verdict = OffsetVerdict(1203e9, 1e5, "T6", 1.0, 2, True)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: None)
    try:
        raw_gps = host_stamped_pair(WALL0 - 1203.0)
        w.add_timing_snapshot(gps_time_ns=raw_gps, rtp_timesnap=RTP_SNAP)
        md = _sidecar(w, verdict)
        assert md["gps_time_ns"] == raw_gps
        assert md["rtp_timesnap"] == RTP_SNAP
        block = md["timing"]
        assert block["plane_source"] == "radiod_pair_judged"
        assert block["offset_ns"] == pytest.approx(1203e9)
        assert block["judge_tier"] == "T6"
        assert block["anchor_rtp"] is None
        # the judge's correction still lands the label at truth
        assert resolve(md, RTP_SNAP) == pytest.approx(WALL0, abs=1e-3)
    finally:
        w.close()


def test_the_sidecar_records_the_chain_delay_in_force(tmp_path):
    verdict = OffsetVerdict(0.0, 1e6, "T6", 1.0, 1, False,
                            chain_delay_ns=-16.35e6,
                            chain_delay_state="confirmed")
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: None)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0), rtp_timesnap=RTP_SNAP
        )
        block = _sidecar(w, verdict)["timing"]
        assert block["chain_delay_ns"] == pytest.approx(-16.35e6)
        assert block["chain_delay_state"] == "confirmed"
    finally:
        w.close()


def test_a_verdict_without_a_record_writes_none(tmp_path):
    verdict = OffsetVerdict(0.0, 1e6, "T4", 1.0, 1, False)
    w = make_writer(tmp_path, judge=StubJudge(verdict), provider=lambda: None)
    try:
        w.add_timing_snapshot(
            gps_time_ns=host_stamped_pair(WALL0), rtp_timesnap=RTP_SNAP
        )
        block = _sidecar(w, verdict)["timing"]
        assert block["chain_delay_ns"] is None
        assert block["chain_delay_state"] is None
    finally:
        w.close()
