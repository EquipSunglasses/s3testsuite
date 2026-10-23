"""Helpers for reading S3 XML responses (namespaces already stripped)."""


def is_true(value):
    return str(value).strip().lower() == 'true'


def keys(root):
    return [c.findtext('Key') for c in root.findall('Contents')]


def common_prefixes(root):
    return [p.findtext('Prefix') for p in root.findall('CommonPrefixes')]


def bucket_names(root):
    return [b.findtext('Name') for b in root.iter('Bucket')]


def tagset(root):
    return dict((t.findtext('Key'), t.findtext('Value')) for t in root.iter('Tag'))


def versions(root):
    out = []
    for child in root:
        if child.tag in ('Version', 'DeleteMarker'):
            out.append({
                'kind': child.tag,
                'Key': child.findtext('Key'),
                'VersionId': child.findtext('VersionId'),
                'IsLatest': is_true(child.findtext('IsLatest')),
                'ETag': (child.findtext('ETag') or '').strip('"'),
                'Size': child.findtext('Size'),
            })
    return out


def truncated(root):
    return is_true(root.findtext('IsTruncated'))
