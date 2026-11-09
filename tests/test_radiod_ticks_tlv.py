"""Decoder for radiod's tick-detector status TLVs (types 218-227).

Fixtures: packets built by radiod's own C encoder (ka9q-radio pilot-detector
head 94d660ee, status.o + pilot.o + tick.o + tick_status.o) through the
helper scripts/radiod_ticks/fixtures/make_tick_packet.c, kept as hex.
Regenerate with the recipe at the top of that file:
    make_tick_packet full | incomplete | mask5
and paste each line into FULL_HEX, INCOMPLETE_HEX, MASK5_HEX below.  The
packets carry 40 profile values (the real report has 840), 1000 envelope
values, an unknown TLV (type 250), and end in EOL.
"""

FULL_HEX = (
    "001202271003081482e92b5962cd150d051cbe991a140a0403dcc50014025dc0fa0107e3081482e92b52070000e2051fe338"
    "ddd4e1021518e00133df8200a441641cf100009206035e920b06bc921c0a1992380d77926010d49292143092d0178c93191a"
    "e8936d1e4293cd219c943724f594ac284e952d2ba595b82efb964e325096ef35a3979b38f598523c4699133f9599de42e39a"
    "b4462e9b9549789c7f4cc19d7450079e73534b9f7c568da08f59cda1ac5d0aa2d26045a402637ea53b66b4a67e69e7a7ca6d"
    "18a91e7046aa7c7371abe37699ad5279beaec97ce0b0497fffb1d1de8200a441e41cf10000c903035ec90e06bcc9300a19c9"
    "680d77c9b710d4ca1c1430ca96178ccb271ae8cbce1e42cc89219ccd5a24f5ce40284ecf3a2ba5d0482efbd1693250d29e35"
    "a3d3e538f5d53e3c46d6a93f95d82442e3d9b0462edb4c4978dcf74cc1deb05007e077534be24a568de42a59cde6155d0ae8"
    "0b6045ea0a637eec1366b4ee2469e7f03c6d18f25a7046f47f7371f6a77699f8d479befb047ce0fd357fffff68dc1846d836"
    "004735ef007fc0000041a4000041ac000000000000db0c3e0000003e8000003ec00000da8207d439dc3372c568c587c5a6c5"
    "c5c5e4c603c622c641c660c67fc69ec6bdc6dcc6fbc71ac739c758c777c796c7b5c7d4c7f3c812c831c850c86fc88ec8adc8"
    "ccc8ebc90ac929c948c967c986c9a5c9c4c9e3ca02ca21ca40ca5fca7eca9dcabccadbcafacb19cb38cb57cb76cb95cbb4cb"
    "d3cbf2cc11cc30cc4fcc6ecc8dccaccccbcceacd09cd28cd47cd66cd85cda4cdc3cde2ce01ce20ce3fce5ece7dce9ccebbce"
    "dacef9cf18cf37cf56cf75cf94cfb3cfd2cff1d010d02fd04ed06dd08cd0abd0cad0e9d108d127d146d165d184d1a3d1c2d1"
    "e1d200d21fd23ed25dd27cd29bd2bad2d9d2f8d317d336d355d374d393d3b2d3d1d3f0d40fd42ed44dd46cd48bd4aad4c9d4"
    "e8d507d526d545d564d583d5a2d5c1d5e0d5ffd61ed63dd65cd67bd69ad6b9d6d8d6f7d716d735d754d773d792d7b1d7d0d7"
    "efd80ed82dd84cd86bd88ad8a9d8c8d8e7d906d925d944d963d982d9a1d9c0d9dfd9feda1dda3cda5bda7ada99dab8dad7da"
    "f6db15db34db53db72db91dbb0dbcfdbeedc0ddc2cdc4bdc6adc89dca8dcc7dce6dd05dd24dd43dd62dd81dda0ddbfdddedd"
    "fdde1cde3bde5ade79de98deb7ded6def5df14df33df52df71df90dfafdfcedfede00ce02be04ae069e088e0a7e0c6e0e5e1"
    "04e123e142e161e180e19fe1bee1dde1fce21be23ae259e278e297e2b6e2d5e2f4e313e332e351e370e38fe3aee3cde3ece4"
    "0be42ae449e468e487e4a6e4c5e4e4e503e522e541e560e57fe59ee5bde5dce5fbe61ae639e658e677e696e6b5e6d4e6f3e7"
    "12e731e750e76fe78ee7ade7cce7ebe80ae829e848e867e886e8a5e8c4e8e3e902e921e940e95fe97ee99de9bce9dbe9faea"
    "19ea38ea57ea76ea95eab4ead3eaf2eb11eb30eb4feb6eeb8debacebcbebeaec09ec28ec47ec66ec85eca4ecc3ece2ed01ed"
    "20ed3fed5eed7ded9cedbbeddaedf9ee18ee37ee56ee75ee94eeb3eed2eef1ef10ef2fef4eef6def8cefabefcaefe9f008f0"
    "27f046f065f084f0a3f0c2f0e1f100f11ff13ef15df17cf19bf1baf1d9f1f8f217f236f255f274f293f2b2f2d1f2f0f30ff3"
    "2ef34df36cf38bf3aaf3c9f3e8f407f426f445f464f483f4a2f4c1f4e0f4fff51ef53df55cf57bf59af5b9f5d8f5f7f616f6"
    "35f654f673f692f6b1f6d0f6eff70ef72df74cf76bf78af7a9f7c8f7e7f806f825f844f863f882f8a1f8c0f8dff8fef91df9"
    "3cf95bf97af999f9b8f9d7f9f6fa15fa34fa53fa72fa91fab0facffaeefb0dfb2cfb4bfb6afb89fba8fbc7fbe6fc05fc24fc"
    "43fc62fc81fca0fcbffcdefcfdfd1cfd3bfd5afd79fd98fdb7fdd6fdf5fe14fe33fe52fe71fe90feaffecefeedff0cff2bff"
    "4aff69ff88ffa7ffc6ffe500040023004200610080009f00be00dd00fc011b013a01590178019701b601d501f40213023202"
    "510270028f02ae02cd02ec030b032a03490368038703a603c503e40403042204410460047f049e04bd04dc04fb051a053905"
    "580577059605b505d405f3061206310650066f068e06ad06cc06eb070a072907480767078607a507c407e308020821084008"
    "5f087e089d08bc08db08fa0919093809570976099509b409d309f20a110a300a4f0a6e0a8d0aac0acb0aea0b090b280b470b"
    "660b850ba40bc30be20c010c200c3f0c5e0c7d0c9c0cbb0cda0cf90d180d370d560d750d940db30dd20df10e100e2f0e4e0e"
    "6d0e8c0eab0eca0ee90f080f270f460f650f840fa30fc20fe11000101f103e105d107c109b10ba10d910f811171136115511"
    "74119311b211d111f0120f122e124d126c128b12aa12c912e81307132613451364138313a213c113e013ff141e143d145c14"
    "7b149a14b914d814f71516153515541573159215b115d015ef160e162d164c166b168a16a916c816e7170617251744176317"
    "8217a117c017df17fe181d183c185b187a189918b818d718f61915193419531972199119b019cf19ee1a0d1a2c1a4b1a6a1a"
    "891aa81ac71ae61b051b241b431b621b811ba01bbf1bde1bfd1c1c1c3b1c5a1c791c981cb71cd61cf51d141d331d521d711d"
    "901daf1dce1ded1e0c1e2b1e4a1e691e881ea71ec61ee51f041f231f421f611f801f9f1fbe1fdd1ffc201b203a2059207820"
    "9720b620d520f42113213221512170218f21ae21cd21ec220b222a22492268228722a622c522e42303232223412360237f23"
    "9e23bd23dc23fb241a243924582477249624b524d424f3251225312550256f258e25ad25cc25eb260a262926482667268626"
    "a526c426e3270227212740275f277e279d27bc27db27fa2819283828572876289528b428d328f229112930294f296e298d29"
    "ac29cb29ea2a092a282a472a662a852aa42ac32ae22b012b202b3f2b5e2b7d2b9c2bbb2bda2bf92c182c372c562c752c942c"
    "b32cd22cf12d102d2f2d4e2d6d2d8c2dab2dca2de92e082e272e462e652e842ea32ec22ee12f002f1f2f3e2f5d2f7c2f9b2f"
    "ba2fd92ff83017303630553074309330b230d130f0310f312e314d316c318b31aa31c931e83207322632453264328332a232"
    "c132e032ff331e333d335c337b339a33b933d833f73416343534543473349234b134d034ef350e352d354c356b358a35a935"
    "c835e73606362536443663368236a136c036df36fe371d373c375b377a379937b837d737f63815383438533872389138b038"
    "cf38ee390d392c394b396a398939a839c739e63a053a243a433a623a813aa03abf3ade3afd3b1c3b3b3b5a3b793b983bb73b"
    "d63bf53c143c333c523c713c903caf3cce3ced3d0c3d2b3d4a3d693d883da73dc63de53e043e233e423e6100"
)

INCOMPLETE_HEX = (
    "001202271003081482e92b5962cd150d051cbe991a140a0403dcc50014025dc0fa0107e3081482e92b52070000e2051fe338"
    "ddd4e1021518e00130df8200a400000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000de8200a400000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000dc187fc000"
    "007fc000007fc00000000000000000000000000000db0c000000000000000000000000da8207d40000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
)

MASK5_HEX = (
    "001202271003081482e92b5962cd150d051cbe991a140a0403dcc50014025dc0fa0107e3081482e92b52070000e2051fe338"
    "ddd4e1021518e00151df8200a441641cf100009206035e920b06bc921c0a1992380d77926010d49292143092d0178c93191a"
    "e8936d1e4293cd219c943724f594ac284e952d2ba595b82efb964e325096ef35a3979b38f598523c4699133f9599de42e39a"
    "b4462e9b9549789c7f4cc19d7450079e73534b9f7c568da08f59cda1ac5d0aa2d26045a402637ea53b66b4a67e69e7a7ca6d"
    "18a91e7046aa7c7371abe37699ad5279beaec97ce0b0497fffb1d1dd8200a4422b15b50000db57035edb6806bcdb9b0a19db"
    "ef0d77dc6410d4dcfa1430ddb1178cde871ae8df7c1e42e08f219ce1be24f5e30a284ee4712ba5e5f02efbe7883250e93635"
    "a3eafa38f5ecd03c46eeb93f95f0b142e3f2b7462ef4ca4978f6e74cc1f90c5007fb38534bfd68568dff9b59cd01ce5d0a03"
    "ff6045062c637e085466b40a7469e70c8b6d180e9670461094737112827699145f79be162a7ce017e07fff1980dc1846d836"
    "007fc00000477fc300411000000000000041b40000db0c3e0000003e8000003ec00000da8207d439dc3372c568c587c5a6c5"
    "c5c5e4c603c622c641c660c67fc69ec6bdc6dcc6fbc71ac739c758c777c796c7b5c7d4c7f3c812c831c850c86fc88ec8adc8"
    "ccc8ebc90ac929c948c967c986c9a5c9c4c9e3ca02ca21ca40ca5fca7eca9dcabccadbcafacb19cb38cb57cb76cb95cbb4cb"
    "d3cbf2cc11cc30cc4fcc6ecc8dccaccccbcceacd09cd28cd47cd66cd85cda4cdc3cde2ce01ce20ce3fce5ece7dce9ccebbce"
    "dacef9cf18cf37cf56cf75cf94cfb3cfd2cff1d010d02fd04ed06dd08cd0abd0cad0e9d108d127d146d165d184d1a3d1c2d1"
    "e1d200d21fd23ed25dd27cd29bd2bad2d9d2f8d317d336d355d374d393d3b2d3d1d3f0d40fd42ed44dd46cd48bd4aad4c9d4"
    "e8d507d526d545d564d583d5a2d5c1d5e0d5ffd61ed63dd65cd67bd69ad6b9d6d8d6f7d716d735d754d773d792d7b1d7d0d7"
    "efd80ed82dd84cd86bd88ad8a9d8c8d8e7d906d925d944d963d982d9a1d9c0d9dfd9feda1dda3cda5bda7ada99dab8dad7da"
    "f6db15db34db53db72db91dbb0dbcfdbeedc0ddc2cdc4bdc6adc89dca8dcc7dce6dd05dd24dd43dd62dd81dda0ddbfdddedd"
    "fdde1cde3bde5ade79de98deb7ded6def5df14df33df52df71df90dfafdfcedfede00ce02be04ae069e088e0a7e0c6e0e5e1"
    "04e123e142e161e180e19fe1bee1dde1fce21be23ae259e278e297e2b6e2d5e2f4e313e332e351e370e38fe3aee3cde3ece4"
    "0be42ae449e468e487e4a6e4c5e4e4e503e522e541e560e57fe59ee5bde5dce5fbe61ae639e658e677e696e6b5e6d4e6f3e7"
    "12e731e750e76fe78ee7ade7cce7ebe80ae829e848e867e886e8a5e8c4e8e3e902e921e940e95fe97ee99de9bce9dbe9faea"
    "19ea38ea57ea76ea95eab4ead3eaf2eb11eb30eb4feb6eeb8debacebcbebeaec09ec28ec47ec66ec85eca4ecc3ece2ed01ed"
    "20ed3fed5eed7ded9cedbbeddaedf9ee18ee37ee56ee75ee94eeb3eed2eef1ef10ef2fef4eef6def8cefabefcaefe9f008f0"
    "27f046f065f084f0a3f0c2f0e1f100f11ff13ef15df17cf19bf1baf1d9f1f8f217f236f255f274f293f2b2f2d1f2f0f30ff3"
    "2ef34df36cf38bf3aaf3c9f3e8f407f426f445f464f483f4a2f4c1f4e0f4fff51ef53df55cf57bf59af5b9f5d8f5f7f616f6"
    "35f654f673f692f6b1f6d0f6eff70ef72df74cf76bf78af7a9f7c8f7e7f806f825f844f863f882f8a1f8c0f8dff8fef91df9"
    "3cf95bf97af999f9b8f9d7f9f6fa15fa34fa53fa72fa91fab0facffaeefb0dfb2cfb4bfb6afb89fba8fbc7fbe6fc05fc24fc"
    "43fc62fc81fca0fcbffcdefcfdfd1cfd3bfd5afd79fd98fdb7fdd6fdf5fe14fe33fe52fe71fe90feaffecefeedff0cff2bff"
    "4aff69ff88ffa7ffc6ffe500040023004200610080009f00be00dd00fc011b013a01590178019701b601d501f40213023202"
    "510270028f02ae02cd02ec030b032a03490368038703a603c503e40403042204410460047f049e04bd04dc04fb051a053905"
    "580577059605b505d405f3061206310650066f068e06ad06cc06eb070a072907480767078607a507c407e308020821084008"
    "5f087e089d08bc08db08fa0919093809570976099509b409d309f20a110a300a4f0a6e0a8d0aac0acb0aea0b090b280b470b"
    "660b850ba40bc30be20c010c200c3f0c5e0c7d0c9c0cbb0cda0cf90d180d370d560d750d940db30dd20df10e100e2f0e4e0e"
    "6d0e8c0eab0eca0ee90f080f270f460f650f840fa30fc20fe11000101f103e105d107c109b10ba10d910f811171136115511"
    "74119311b211d111f0120f122e124d126c128b12aa12c912e81307132613451364138313a213c113e013ff141e143d145c14"
    "7b149a14b914d814f71516153515541573159215b115d015ef160e162d164c166b168a16a916c816e7170617251744176317"
    "8217a117c017df17fe181d183c185b187a189918b818d718f61915193419531972199119b019cf19ee1a0d1a2c1a4b1a6a1a"
    "891aa81ac71ae61b051b241b431b621b811ba01bbf1bde1bfd1c1c1c3b1c5a1c791c981cb71cd61cf51d141d331d521d711d"
    "901daf1dce1ded1e0c1e2b1e4a1e691e881ea71ec61ee51f041f231f421f611f801f9f1fbe1fdd1ffc201b203a2059207820"
    "9720b620d520f42113213221512170218f21ae21cd21ec220b222a22492268228722a622c522e42303232223412360237f23"
    "9e23bd23dc23fb241a243924582477249624b524d424f3251225312550256f258e25ad25cc25eb260a262926482667268626"
    "a526c426e3270227212740275f277e279d27bc27db27fa2819283828572876289528b428d328f229112930294f296e298d29"
    "ac29cb29ea2a092a282a472a662a852aa42ac32ae22b012b202b3f2b5e2b7d2b9c2bbb2bda2bf92c182c372c562c752c942c"
    "b32cd22cf12d102d2f2d4e2d6d2d8c2dab2dca2de92e082e272e462e652e842ea32ec22ee12f002f1f2f3e2f5d2f7c2f9b2f"
    "ba2fd92ff83017303630553074309330b230d130f0310f312e314d316c318b31aa31c931e83207322632453264328332a232"
    "c132e032ff331e333d335c337b339a33b933d833f73416343534543473349234b134d034ef350e352d354c356b358a35a935"
    "c835e73606362536443663368236a136c036df36fe371d373c375b377a379937b837d737f63815383438533872389138b038"
    "cf38ee390d392c394b396a398939a839c739e63a053a243a433a623a813aa03abf3ade3afd3b1c3b3b3b5a3b793b983bb73b"
    "d63bf53c143c333c523c713c903caf3cce3ced3d0c3d2b3d4a3d693d883da73dc63de53e043e233e423e6100"
)

import math
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from radiod_ticks import tlv_ticks  # noqa: E402

NPROF = 40
DECIM = 2700
BASE = 123456789012


def _pkt(h):
    return bytes.fromhex(h)


def _expect_profile(k):
    i = np.arange(NPROF)
    re = 37.5 * (k + 1) * np.sin(0.01 * i)
    im = -12.25 * np.cos(0.02 * i * (k + 1))
    return re + 1j * im


def _tol(k):
    p = _expect_profile(k)
    return max(np.abs(p.real).max(), np.abs(p.imag).max()) / 32767 * 1.01


def test_full_report_every_field():
    d = tlv_ticks.decode_ticks(_pkt(FULL_HEX))
    # base fields still decode, unknown type 250 skipped
    assert d["ssrc"] == 10000 and d["input_samprate"] == 64800000
    assert d["tick_second_utc"] == 1478000000000000000
    assert d["tick_window_start"] == BASE + (5000000 - 240) * DECIM
    assert d["tick_step"] == 2 * DECIM
    assert d["tick_flags"] == 1 | 2 | (3 << 4)
    assert d["tick_complete"] is True and d["tick_used_pilot"] is True
    assert d["tick_templates"] == 3
    for k, name in enumerate(("wwv", "wwvh")):
        p = d[f"tick_profile_{name}"]
        assert p.dtype == np.complex64 and p.shape == (NPROF,)
        assert np.abs(p - _expect_profile(k)).max() <= _tol(k)
    assert "tick_profile_bpm" not in d
    pk = d["tick_peaks"]
    assert pk.dtype == np.float32 and pk.shape == (3, 2)      # [template][offset, snr]
    for k in (0, 1):
        assert pk[k, 0] == np.float32((10.25 + 7 * k) * DECIM)
        assert pk[k, 1] == np.float32(20.5 + k)
    assert np.isnan(pk[2, 0]) and pk[2, 1] == 0      # bpm not in the mask
    mk = d["tick_markers"]
    assert mk.dtype == np.float32 and list(mk) == [0.125, 0.25, 0.375]
    env = d["tick_envelope"]
    assert env.dtype == np.float32 and env.shape == (1000,)
    want = (np.arange(1000) * 31 - 15000) * np.float32(0.00042)
    assert np.allclose(env, want, rtol=1e-5, atol=0)


def test_incomplete_report_nan_peaks_zero_scales():
    d = tlv_ticks.decode_ticks(_pkt(INCOMPLETE_HEX))
    assert d["tick_complete"] is False and d["tick_used_pilot"] is False
    assert d["tick_flags"] == 3 << 4
    for name in ("wwv", "wwvh"):
        p = d[f"tick_profile_{name}"]
        assert p.shape == (NPROF,) and not p.any()
    assert np.isnan(d["tick_peaks"][:, 0]).all()
    assert not np.isnan(d["tick_peaks"][:, 1]).any()
    assert d["tick_envelope"].shape == (1000,) and np.isnan(d["tick_envelope"]).all()


def test_mask_wwv_bpm_leaves_wwvh_absent():
    d = tlv_ticks.decode_ticks(_pkt(MASK5_HEX))
    assert d["tick_templates"] == 5 and d["tick_flags"] == 1 | (5 << 4)
    assert "tick_profile_wwv" in d and "tick_profile_bpm" in d
    assert "tick_profile_wwvh" not in d
    pk = d["tick_peaks"]
    assert np.isnan(pk[1, 0]) and np.isfinite(pk[0, 0]) and np.isfinite(pk[2, 0])
    assert pk[0, 1] == 9.0
    assert np.abs(d["tick_profile_bpm"] - _expect_profile(2)).max() <= _tol(2)


def test_truncated_mid_tick_group_keeps_what_came_before():
    pkt = _pkt(FULL_HEX)
    # cut inside the WWVH profile TLV: type 222 appears once, after the WWV profile
    cut = pkt.index(bytes([222, 0x82]))
    d = tlv_ticks.decode_ticks(pkt[:cut + 20])
    assert d["ssrc"] == 10000
    assert d["tick_second_utc"] == 1478000000000000000
    assert d["tick_flags"] == 1 | 2 | (3 << 4)
    assert "tick_profile_wwv" in d
    assert "tick_profile_wwvh" not in d
    for k in ("tick_peaks", "tick_markers", "tick_envelope"):
        assert k not in d


def test_non_status_packet_raises():
    with pytest.raises(ValueError):
        tlv_ticks.decode_ticks(b"\x01\x02")


def test_malformed_profile_length_is_skipped_not_fatal():
    # a profile TLV whose body is not 4 + 4n bytes is ignored; the rest decodes
    bad = bytes([0, 223, 5]) + b"\0" * 5
    tail = bytes([219, 12]) + struct.pack(">3f", 1, 2, 3) + bytes([0])
    d = tlv_ticks.decode_ticks(bad + tail)
    assert "tick_profile_wwv" not in d
    assert list(d["tick_markers"]) == [1, 2, 3]


# ---------------------------------------------------------------- the logger
import json  # noqa: E402
import socket  # noqa: E402

from radiod_ticks import listen_ticks  # noqa: E402

GPS_UNIX = tlv_ticks.GPS_TO_UNIX_NS
# 2026-11-09 12:00:00 UTC on the GPS scale as radiod names it
T0 = 1_794_225_600 * 10**9 - GPS_UNIX


def _at(sec_offset, ssrc=10000, hexpkt=None):
    """The fixture packet with its TICK_SECOND_UTC and SSRC rewritten."""
    pkt = bytearray(_pkt(hexpkt or FULL_HEX))
    old = (1478000000000000000).to_bytes(8, "big")
    i = pkt.index(bytes([227, 8]) + old) + 2
    pkt[i:i + 8] = (T0 + sec_offset * 10**9).to_bytes(8, "big")
    j = pkt.index(bytes([18, 2]) + (10000).to_bytes(2, "big")) + 2
    pkt[j:j + 2] = ssrc.to_bytes(2, "big")
    return bytes(pkt)


def _log(tmp_path, packets):
    lg = listen_ticks.TickLogger(tmp_path)
    for p in packets:
        lg.add(tlv_ticks.decode_ticks(p), "239.1.2.3", now=1.0)
    return lg


def _index(tmp_path, ssrc=10000):
    rows = []
    for f in sorted(tmp_path.glob(f"index-{ssrc}-*.jsonl")):
        rows += [json.loads(x) for x in f.read_text().splitlines()]
    return rows


def test_logger_minute_file_and_index(tmp_path):
    # seconds 58, 59 of one minute, then 0 of the next
    lg = _log(tmp_path, [_at(58), _at(59), _at(60, hexpkt=MASK5_HEX)])
    lg.flush()
    f1 = tmp_path / "ticks-10000-20261109T1200Z.npz"
    f2 = tmp_path / "ticks-10000-20261109T1201Z.npz"
    assert f1.exists() and f2.exists()
    z = np.load(f1)
    assert z["profiles"].dtype == np.complex64 and z["profiles"].shape == (2, 3, NPROF)
    assert z["envelope"].dtype == np.float32 and z["envelope"].shape == (2, 1000)
    assert list(z["second_utc"]) == [T0 + 58 * 10**9, T0 + 59 * 10**9]
    assert z["peaks"].shape == (2, 3, 2) and z["markers"].shape == (2, 3)
    assert list(z["flags"]) == [0x33, 0x33]
    assert list(z["step"]) == [2 * DECIM] * 2
    assert list(z["window_start"]) == [BASE + (5000000 - 240) * DECIM] * 2
    assert np.abs(z["profiles"][0, 0] - _expect_profile(0)).max() <= _tol(0)
    assert not z["profiles"][0, 2].any()                       # bpm not in mask
    z2 = np.load(f2)
    assert list(z2["nprofile"][0]) == [NPROF, 0, NPROF]        # mask 0b101
    assert not z2["profiles"][0, 1].any()
    rows = _index(tmp_path)
    assert len(rows) == 3
    assert all("profiles" not in r and "envelope" not in r for r in rows)
    assert rows[0]["unix_second"] == 1794225600 + 58 and rows[0]["late"] is False
    assert rows[0]["npz"] == f1.name and rows[2]["npz"] == f2.name
    assert rows[2]["nprofile"] == {"wwv": NPROF, "bpm": NPROF}
    assert rows[2]["peaks"][1][0] is None                     # absent wwvh


def test_logger_dedupes_on_ssrc_and_second(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    d = tlv_ticks.decode_ticks(_at(5))
    assert lg.add(d) is True
    assert lg.add(tlv_ticks.decode_ticks(_at(5))) is False        # second destination
    assert lg.add(tlv_ticks.decode_ticks(_at(5, ssrc=10001))) is True   # other channel
    lg.close()
    assert len(_index(tmp_path)) == 1 and len(_index(tmp_path, 10001)) == 1
    z = np.load(tmp_path / "ticks-10000-20261109T1200Z.npz")
    assert z["profiles"].shape[0] == 1


def test_logger_late_report_goes_to_index_only(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    lg.add(tlv_ticks.decode_ticks(_at(121)))
    lg.add(tlv_ticks.decode_ticks(_at(59)))                       # two minutes behind
    lg.close()
    rows = _index(tmp_path)
    assert [r["late"] for r in rows] == [False, True]
    z = np.load(tmp_path / "ticks-10000-20261109T1202Z.npz")
    assert z["profiles"].shape[0] == 1


def test_logger_keeps_one_minute_per_channel(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    for m in range(5):
        for s in (0, 30):
            lg.add(tlv_ticks.decode_ticks(_at(60 * m + s)))
        assert len(lg.chan[10000].open) <= 2
    # minutes 0-3 written (each next minute is 30 s old); minute 4 still buffered
    assert len(list(tmp_path.glob("ticks-10000-*.npz"))) == 4


def test_logger_ignores_packets_without_a_tick_group(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    assert lg.add({"ssrc": 10000, "gps_time": 5}) is False
    assert not _index(tmp_path)


class _FakeSock:
    def __init__(self, packets):
        self.packets = list(packets)
        self.sizes = []

    def recvmsg(self, bufsize, ancsize):
        self.sizes.append(bufsize)
        if not self.packets:
            raise KeyboardInterrupt
        p = self.packets.pop(0)
        if isinstance(p, BaseException):
            raise p
        return p[:bufsize], [], 0, ("10.0.0.1", 5006)


def test_run_filters_ssrc_uses_big_buffer_and_survives_quiet(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    sock = _FakeSock([_at(1), _at(2, ssrc=999), b"\x01junk", socket.timeout(), _at(3)])
    n = listen_ticks.run(sock, lg, [10000], err=open(tmp_path / "err", "w"))
    lg.close()
    assert n == 2
    assert min(sock.sizes) >= 65536
    assert len(_index(tmp_path)) == 2 and not _index(tmp_path, 999)


def _seconds(tmp_path, stamp="20261109T1200Z", ssrc=10000):
    z = np.load(tmp_path / f"ticks-{ssrc}-{stamp}.npz")
    return [(int(x) - T0) // 10**9 for x in z["second_utc"]]


def test_restart_mid_minute_keeps_both_runs(tmp_path):
    a = listen_ticks.TickLogger(tmp_path)
    a.add(tlv_ticks.decode_ticks(_at(10)))
    a.add(tlv_ticks.decode_ticks(_at(11)))
    a.close()                                           # run one ends mid-minute
    b = listen_ticks.TickLogger(tmp_path)
    b.add(tlv_ticks.decode_ticks(_at(11, hexpkt=MASK5_HEX)))   # duplicate second: new wins
    b.add(tlv_ticks.decode_ticks(_at(12)))
    b.close()
    assert _seconds(tmp_path) == [10, 11, 12]
    z = np.load(tmp_path / "ticks-10000-20261109T1200Z.npz")
    assert list(z["nprofile"][1]) == [NPROF, 0, NPROF]  # second 11 is the newer packet
    assert list(z["nprofile"][0]) == [NPROF, NPROF, 0]
    assert np.abs(z["profiles"][0, 1] - _expect_profile(1)).max() <= _tol(1)


def test_quiet_flush_then_same_minute_report_keeps_both(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    lg.add(tlv_ticks.decode_ticks(_at(10)))
    lg.flush()
    lg.add(tlv_ticks.decode_ticks(_at(20)))
    lg.close()
    assert _seconds(tmp_path) == [10, 20]


def test_reordered_second_59_lands_in_its_minute(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    lg.add(tlv_ticks.decode_ticks(_at(58)))
    lg.add(tlv_ticks.decode_ticks(_at(60)))             # next minute first
    assert not (tmp_path / "ticks-10000-20261109T1200Z.npz").exists()   # held back
    lg.add(tlv_ticks.decode_ticks(_at(59)))             # then the straggler
    assert _seconds(tmp_path) == [58, 59]
    lg.close()
    assert _seconds(tmp_path, "20261109T1201Z") == [60]
    assert [r["late"] for r in _index(tmp_path)] == [False, False, False]


def test_previous_minute_closes_once_next_is_aged(tmp_path):
    lg = listen_ticks.TickLogger(tmp_path)
    lg.add(tlv_ticks.decode_ticks(_at(58)))
    lg.add(tlv_ticks.decode_ticks(_at(60)))
    lg.add(tlv_ticks.decode_ticks(_at(66)))             # next minute is 6 s old
    assert _seconds(tmp_path) == [58]
    lg.add(tlv_ticks.decode_ticks(_at(59)))             # arrives after the close: merged
    lg.close()
    assert _seconds(tmp_path) == [58, 59]


@pytest.mark.parametrize("junk", ["truncated", "empty", "pk_junk"])
def test_corrupt_existing_minute_file_is_kept_aside(tmp_path, junk):
    # a real minute file, then damaged
    a = listen_ticks.TickLogger(tmp_path)
    a.add(tlv_ticks.decode_ticks(_at(10)))
    a.close()
    f = tmp_path / "ticks-10000-20261109T1200Z.npz"
    good = f.read_bytes()
    f.write_bytes({"truncated": good[:len(good) // 2], "empty": b"",
                   "pk_junk": b"PK\x03\x04" + b"\xff" * 50}[junk])
    damaged = f.read_bytes()
    err = open(tmp_path / "err", "w")
    b = listen_ticks.TickLogger(tmp_path, err=err)
    assert b.add(tlv_ticks.decode_ticks(_at(20))) is True
    b.close()                                           # must not raise
    assert _seconds(tmp_path) == [20]                   # fresh file, new run only
    aside = list(tmp_path.glob("ticks-10000-20261109T1200Z.npz.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == damaged
