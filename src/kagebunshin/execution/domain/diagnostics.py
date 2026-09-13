"""Bounded observations for repair; process output is data, never authority."""
from hashlib import sha256


def diagnostic(call, remaining_bytes):
    receipt = call.get('receipt', {})
    if call['operation'] == 'apply_changes':
        return {'change': receipt.get('change')}, remaining_bytes
    if call['operation'] != 'run_check':
        return {}, remaining_bytes
    check = receipt.get('check', {})
    result = {key: check.get(key) for key in ('check_id', 'definition_hash', 'input_hash', 'passed', 'supervision')}
    process = check.get('process', {})
    result['reason'] = process.get('reason')
    result['returncode'] = process.get('returncode')
    for stream in ('stdout', 'stderr'):
        value = process.get(stream, '')
        if type(value) is not str:
            value = ''
        encoded = value.encode('utf-8')
        excerpt = encoded[:remaining_bytes].decode('utf-8', errors='ignore')
        used = len(excerpt.encode('utf-8'))
        remaining_bytes -= used
        result[stream] = {'excerpt': excerpt, 'truncated': used < len(encoded),
                          'text_sha256': sha256(encoded).hexdigest(),
                          'captured_bytes_sha256': process.get(stream + '_sha256')}
    return {'check': result, 'output_is_untrusted': True}, remaining_bytes
