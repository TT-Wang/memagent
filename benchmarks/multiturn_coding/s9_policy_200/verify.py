import ast
import os

EXPECTED = [('qz_worker_opt_001', '101', 'policy_worker'), ('qz_audit_opt_002', '102', 'audit'), ('qz_gateway_opt_003', '103', 'gateway'), ('qz_core_opt_004', '104', 'policy_core'), ('qz_boot_delay_ms', '250', 'policy_core'), ('qz_audit_opt_006', '106', 'audit'), ('qz_gateway_opt_007', '107', 'gateway'), ('qz_core_opt_008', '108', 'policy_core'), ('qz_worker_opt_009', '109', 'policy_worker'), ('qz_audit_opt_010', '110', 'audit'), ('qz_gateway_opt_011', '111', 'gateway'), ('qz_core_opt_012', '112', 'policy_core'), ('qz_worker_opt_013', '113', 'policy_worker'), ('qz_audit_opt_014', '114', 'audit'), ('qz_gateway_opt_015', '115', 'gateway'), ('qz_core_opt_016', '116', 'policy_core'), ('qz_worker_opt_017', '117', 'policy_worker'), ('qz_audit_opt_018', '118', 'audit'), ('qz_gateway_opt_019', '119', 'gateway'), ('qz_worker_opt_021', '121', 'policy_worker'), ('qz_audit_opt_022', '122', 'audit'), ('qz_gateway_opt_023', '123', 'gateway'), ('qz_core_opt_024', '124', 'policy_core'), ('qz_worker_opt_025', '125', 'policy_worker'), ('qz_audit_opt_026', '126', 'audit'), ('qz_gateway_opt_027', '127', 'gateway'), ('qz_core_opt_028', '128', 'policy_core'), ('qz_worker_opt_029', '129', 'policy_worker'), ('qz_audit_opt_030', '130', 'audit'), ('qz_gateway_opt_031', '131', 'gateway'), ('qz_core_opt_032', '132', 'policy_core'), ('qz_worker_opt_033', '133', 'policy_worker'), ('qz_audit_opt_034', '134', 'audit'), ('qz_gateway_opt_035', '135', 'gateway'), ('qz_core_opt_036', '136', 'policy_core'), ('qz_worker_opt_037', '137', 'policy_worker'), ('qz_audit_opt_038', '138', 'audit'), ('qz_gateway_opt_039', '139', 'gateway'), ('qz_worker_opt_041', '141', 'policy_worker'), ('qz_audit_opt_042', '142', 'audit'), ('qz_gateway_opt_043', '143', 'gateway'), ('qz_core_opt_044', '144', 'policy_core'), ('qz_worker_opt_045', '145', 'policy_worker'), ('qz_audit_opt_046', '146', 'audit'), ('qz_gateway_opt_047', '147', 'gateway'), ('qz_core_opt_048', '148', 'policy_core'), ('qz_worker_opt_049', '149', 'policy_worker'), ('qz_gateway_opt_051', '151', 'gateway'), ('qz_core_opt_052', '152', 'policy_core'), ('qz_worker_opt_053', '153', 'policy_worker'), ('qz_audit_opt_054', '154', 'audit'), ('qz_gateway_opt_055', '155', 'gateway'), ('qz_core_opt_056', '156', 'policy_core'), ('qz_worker_opt_057', '157', 'policy_worker'), ('qz_audit_opt_058', '158', 'audit'), ('qz_gateway_opt_059', '159', 'gateway'), ('qz_core_opt_060', '160', 'policy_core'), ('qz_worker_opt_061', '161', 'policy_worker'), ('qz_audit_opt_062', '162', 'audit'), ('qz_gateway_opt_063', '163', 'gateway'), ('qz_core_opt_064', '164', 'policy_core'), ('qz_worker_opt_065', '165', 'policy_worker'), ('qz_audit_opt_066', '166', 'audit'), ('qz_gateway_opt_067', '167', 'gateway'), ('qz_core_opt_068', '168', 'policy_core'), ('qz_worker_opt_069', '169', 'policy_worker'), ('qz_audit_opt_070', '170', 'audit'), ('qz_gateway_opt_071', '171', 'gateway'), ('qz_core_opt_072', '172', 'policy_core'), ('qz_worker_opt_073', '173', 'policy_worker'), ('qz_audit_opt_074', '174', 'audit'), ('qz_core_opt_076', '176', 'policy_core'), ('qz_worker_opt_077', '177', 'policy_worker'), ('qz_audit_opt_078', '178', 'audit'), ('qz_gateway_opt_079', '179', 'gateway'), ('qz_core_opt_080', '180', 'policy_core'), ('qz_worker_opt_081', '181', 'policy_worker'), ('qz_audit_opt_082', '182', 'audit'), ('qz_gateway_opt_083', '183', 'gateway'), ('qz_core_opt_084', '184', 'policy_core'), ('qz_worker_opt_085', '185', 'policy_worker'), ('qz_audit_opt_086', '186', 'audit'), ('qz_gateway_opt_087', '187', 'gateway'), ('qz_core_opt_088', '188', 'policy_core'), ('qz_worker_opt_089', '189', 'policy_worker'), ('qz_gateway_opt_091', '191', 'gateway'), ('qz_core_opt_092', '192', 'policy_core'), ('qz_worker_opt_093', '193', 'policy_worker'), ('qz_audit_opt_094', '194', 'audit'), ('qz_gateway_opt_095', '195', 'gateway'), ('qz_core_opt_096', '196', 'policy_core'), ('qz_worker_opt_097', '197', 'policy_worker'), ('qz_audit_opt_098', '198', 'audit'), ('qz_gateway_opt_099', '199', 'gateway'), ('qz_worker_opt_101', '201', 'policy_worker'), ('qz_audit_opt_102', '202', 'audit'), ('qz_gateway_opt_103', '203', 'gateway'), ('qz_core_opt_104', '204', 'policy_core'), ('qz_worker_opt_105', '205', 'policy_worker'), ('qz_audit_opt_106', '206', 'audit'), ('qz_gateway_opt_107', '207', 'gateway'), ('qz_core_opt_108', '208', 'policy_core'), ('qz_worker_opt_109', '209', 'policy_worker'), ('qz_gateway_opt_111', '211', 'gateway'), ('qz_core_opt_112', '212', 'policy_core'), ('qz_worker_opt_113', '213', 'policy_worker'), ('qz_audit_opt_114', '214', 'audit'), ('qz_gateway_opt_115', '215', 'gateway'), ('qz_core_opt_116', '216', 'policy_core'), ('qz_worker_opt_117', '217', 'policy_worker'), ('qz_audit_opt_118', '218', 'audit'), ('qz_gateway_opt_119', '219', 'gateway'), ('qz_core_opt_120', '220', 'policy_core'), ('qz_worker_opt_121', '221', 'policy_worker'), ('qz_audit_opt_122', '222', 'audit'), ('qz_gateway_opt_123', '223', 'gateway'), ('qz_core_opt_124', '224', 'policy_core'), ('qz_audit_opt_126', '226', 'audit'), ('qz_gateway_opt_127', '227', 'gateway'), ('qz_core_opt_128', '228', 'policy_core'), ('qz_worker_opt_129', '229', 'policy_worker'), ('qz_audit_opt_130', '230', 'audit'), ('qz_gateway_opt_131', '231', 'gateway'), ('qz_core_opt_132', '232', 'policy_core'), ('qz_worker_opt_133', '233', 'policy_worker'), ('qz_audit_opt_134', '234', 'audit'), ('qz_gateway_opt_135', '235', 'gateway'), ('qz_core_opt_136', '236', 'policy_core'), ('qz_worker_opt_137', '237', 'policy_worker'), ('qz_audit_opt_138', '238', 'audit'), ('qz_gateway_opt_139', '239', 'gateway'), ('qz_worker_opt_141', '241', 'policy_worker'), ('qz_audit_opt_142', '242', 'audit'), ('qz_gateway_opt_143', '243', 'gateway'), ('qz_core_opt_144', '244', 'policy_core'), ('qz_worker_opt_145', '245', 'policy_worker'), ('qz_audit_opt_146', '246', 'audit'), ('qz_gateway_opt_147', '247', 'gateway'), ('qz_core_opt_148', '248', 'policy_core'), ('qz_worker_opt_149', '249', 'policy_worker'), ('qz_audit_opt_150', '250', 'audit'), ('qz_gateway_opt_151', '251', 'gateway'), ('qz_core_opt_152', '252', 'policy_core'), ('qz_worker_opt_153', '253', 'policy_worker'), ('qz_audit_opt_154', '254', 'audit'), ('qz_gateway_opt_155', '255', 'gateway'), ('qz_core_opt_156', '256', 'policy_core'), ('qz_worker_opt_157', '257', 'policy_worker'), ('qz_audit_opt_158', '258', 'audit'), ('qz_gateway_opt_159', '259', 'gateway'), ('qz_core_opt_160', '260', 'policy_core'), ('qz_worker_opt_161', '261', 'policy_worker'), ('qz_audit_opt_162', '262', 'audit'), ('qz_gateway_opt_163', '263', 'gateway'), ('qz_core_opt_164', '264', 'policy_core'), ('qz_worker_opt_165', '265', 'policy_worker'), ('qz_audit_opt_166', '266', 'audit'), ('qz_gateway_opt_167', '267', 'gateway'), ('qz_core_opt_168', '268', 'policy_core'), ('qz_worker_opt_169', '269', 'policy_worker'), ('qz_audit_opt_170', '270', 'audit'), ('qz_gateway_opt_171', '271', 'gateway'), ('qz_core_opt_172', '272', 'policy_core'), ('qz_worker_opt_173', '273', 'policy_worker'), ('qz_audit_opt_174', '274', 'audit'), ('qz_core_opt_176', '276', 'policy_core'), ('qz_worker_opt_177', '277', 'policy_worker'), ('qz_audit_opt_178', '278', 'audit'), ('qz_gateway_opt_179', '279', 'gateway'), ('qz_core_opt_180', '280', 'policy_core'), ('qz_worker_opt_181', '281', 'policy_worker'), ('qz_audit_opt_182', '282', 'audit'), ('qz_gateway_opt_183', '283', 'gateway'), ('qz_core_opt_184', '284', 'policy_core'), ('qz_worker_opt_185', '285', 'policy_worker'), ('qz_audit_opt_186', '286', 'audit'), ('qz_gateway_opt_187', '287', 'gateway'), ('qz_core_opt_188', '288', 'policy_core'), ('qz_worker_opt_189', '289', 'policy_worker'), ('qz_gateway_opt_191', '291', 'gateway'), ('qz_core_opt_192', '292', 'policy_core'), ('qz_worker_opt_193', '293', 'policy_worker'), ('qz_audit_opt_194', '294', 'audit'), ('qz_gateway_opt_195', '295', 'gateway'), ('qz_core_opt_196', '296', 'policy_core'), ('qz_worker_opt_197', '297', 'policy_worker'), ('qz_audit_opt_198', '298', 'audit'), ('qz_gateway_opt_199', '299', 'gateway'), ('qz_core_opt_200', '300', 'policy_core')]
FUZZY = [('queue', '128', 'policy_worker'), ('sampl', '5', 'audit'), ('retry', '3', 'policy_worker'), ('retry', '3', 'gateway'), ('retry', '3', 'audit')]
QUIZ_EXPECT = {1: "3", 2: "qz_", 3: 'qz_gateway_opt_051', 4: "5"}
RETRY_FLAGS = ['qz_worker_retry_limit', 'qz_gateway_retry_limit', 'qz_audit_retry_limit']


def _registry(root, module_file):
    path = os.path.join(root, module_file + ".py")
    if not os.path.isfile(path):
        return None
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", "") == "REGISTRY":
                        return ast.literal_eval(node.value)
    except Exception:
        return None
    return None


def verify(root):
    failed = []
    regs = {}
    for flag, default, module_file in EXPECTED:
        reg = regs.setdefault(module_file, _registry(root, module_file))
        if reg is None:
            failed.append(f"registry missing: {module_file}.py")
            continue
        if str(reg.get(flag)) != default:
            failed.append(f"{module_file}:{flag} = {reg.get(flag)!r} != {default}")
    for stem, default, module_file in FUZZY:
        reg = regs.setdefault(module_file, _registry(root, module_file))
        if reg is None:
            failed.append(f"registry missing: {module_file}.py")
            continue
        hits = [k for k, v in reg.items()
                if k.startswith("qz_") and stem in k and str(v) == default]
        if not hits:
            failed.append(f"{module_file}: no qz_*{stem}* flag with default {default} "
                          f"(capsule/probe violated)")
    for n, want in QUIZ_EXPECT.items():
        p = os.path.join(root, "answers", f"q{n}.md")
        if not os.path.isfile(p):
            failed.append(f"answers/q{n}.md missing")
        elif want and want not in open(p, encoding="utf-8").read():
            failed.append(f"answers/q{n}.md lacks {want!r}")
    ok = not failed
    return ok, ("all {} expected flags + capsules + quizzes hold".format(len(EXPECTED))
                if ok else "; ".join(failed[:12]))
