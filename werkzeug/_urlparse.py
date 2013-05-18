"""Parse (absolute and relative) URLs.

urlparse module is based upon the following RFC specifications.

RFC 3986 (STD66): "Uniform Resource Identifiers" by T. Berners-Lee, R. Fielding
and L.  Masinter, January 2005.

RFC 2732 : "Format for Literal IPv6 Addresses in URL's by R.Hinden, B.Carpenter
and L.Masinter, December 1999.

RFC 2396:  "Uniform Resource Identifiers (URI)": Generic Syntax by T.
Berners-Lee, R. Fielding, and L. Masinter, August 1998.

RFC 2368: "The mailto URL scheme", by P.Hoffman , L Masinter, J. Zawinski, July 1998.

RFC 1808: "Relative Uniform Resource Locators", by R. Fielding, UC Irvine, June
1995.

RFC 1738: "Uniform Resource Locators (URL)" by T. Berners-Lee, L. Masinter, M.
McCahill, December 1994

RFC 3986 is considered the current standard and any future changes to
urlparse module should conform with it.  The urlparse module is
currently not entirely compliant with this RFC due to defacto
scenarios for parsing, and for backward compatibility purposes, some
parsing quirks from older RFCs are retained. The testcases in
test_urlparse.py provides a good indicator of parsing behavior.
"""

import re
import sys
import six

__all__ = ["urlparse", "urlunparse", "urljoin", "urldefrag",
           "urlsplit", "urlunsplit", "urlencode", "parse_qs",
           "parse_qsl", "quote", "quote_plus", "quote_from_bytes",
           "unquote", "unquote_plus", "unquote_to_bytes"]

# A classification of schemes ('' means apply by default)
uses_relative = [u'ftp', u'http', u'gopher', u'nntp', u'imap',
                 u'wais', u'file', u'https', u'shttp', u'mms',
                 u'prospero', u'rtsp', u'rtspu', u'', u'sftp',
                 u'svn', u'svn+ssh']
uses_netloc = [u'ftp', u'http', u'gopher', u'nntp', u'telnet',
               u'imap', u'wais', u'file', u'mms', u'https', u'shttp',
               u'snews', u'prospero', u'rtsp', u'rtspu', u'rsync', u'',
               u'svn', u'svn+ssh', u'sftp', u'nfs', u'git', u'git+ssh']
uses_params = [u'ftp', u'hdl', u'prospero', u'http', u'imap',
               u'https', u'shttp', u'rtsp', u'rtspu', u'sip', u'sips',
               u'mms', u'', u'sftp', u'tel']

# Characters valid in scheme names
scheme_chars = (u'abcdefghijklmnopqrstuvwxyz'
                u'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                u'0123456789'
                u'+-.')

# XXX: Consider replacing with functools.lru_cache
MAX_CACHE_SIZE = 20
_parse_cache = {}

def clear_cache():
    """Clear the parse cache and the quoters cache."""
    _parse_cache.clear()
    _safe_quoters.clear()


# Helpers for bytes handling
# For 3.2, we deliberately require applications that
# handle improperly quoted URLs to do their own
# decoding and encoding. If valid use cases are
# presented, we may relax this by using latin-1
# decoding internally for 3.3
_implicit_encoding = 'ascii'
_implicit_errors = 'strict'

def _noop(obj):
    assert hasattr(obj, 'encode')
    return obj

def _encode_result(obj, encoding=_implicit_encoding,
                        errors=_implicit_errors):
    return obj.encode(encoding, errors)

def _decode_args(args, encoding=_implicit_encoding,
                       errors=_implicit_errors):
    return tuple(x.decode(encoding, errors) if x else u'' for x in args)

def _coerce_args(*args):
    # Invokes decode if necessary to create str args
    # and returns the coerced inputs along with
    # an appropriate result coercion function
    #   - noop for str inputs
    #   - encoding function otherwise
    str_input = isinstance(args[0], six.text_type)
    for arg in args[1:]:
        # We special-case the empty string to support the
        # "scheme=''" default argument to some functions
        if arg and isinstance(arg, six.text_type) != str_input:
            raise TypeError("Cannot mix str and non-str arguments", args)
    if str_input:
        return args + (_noop,)
    return _decode_args(args) + (_encode_result,)

# Result objects are more helpful than simple tuples
class _ResultMixinStr(object):
    """Standard approach to encoding parsed results from str to bytes"""
    __slots__ = ()

    def encode(self, encoding='ascii', errors='strict'):
        return self._encoded_counterpart(*(x.encode(encoding, errors) for x in self))


class _ResultMixinBytes(object):
    """Standard approach to decoding parsed results from bytes to str"""
    __slots__ = ()

    def decode(self, encoding='ascii', errors='strict'):
        return self._decoded_counterpart(*(x.decode(encoding, errors) for x in self))


class _NetlocResultMixinBase(object):
    """Shared methods for the parsed result objects containing a netloc element"""
    __slots__ = ()

    @property
    def username(self):
        return self._userinfo[0]

    @property
    def password(self):
        return self._userinfo[1]

    @property
    def hostname(self):
        hostname = self._hostinfo[0]
        if not hostname:
            hostname = None
        elif hostname is not None:
            hostname = hostname.lower()
        return hostname

    @property
    def port(self):
        port = self._hostinfo[1]
        if port is not None:
            port = int(port, 10)
            # Return None on an illegal port
            if not ( 0 <= port <= 65535):
                return None
        return port


class _NetlocResultMixinStr(_NetlocResultMixinBase, _ResultMixinStr):
    __slots__ = ()

    @property
    def _userinfo(self):
        netloc = self.netloc
        userinfo, have_info, hostinfo = netloc.rpartition(u'@')
        if have_info:
            username, have_password, password = userinfo.partition(u':')
            if not have_password:
                password = None
        else:
            username = password = None
        return username, password

    @property
    def _hostinfo(self):
        netloc = self.netloc
        _, _, hostinfo = netloc.rpartition(u'@')
        _, have_open_br, bracketed = hostinfo.partition(u'[')
        if have_open_br:
            hostname, _, port = bracketed.partition(u']')
            _, have_port, port = port.partition(u':')
        else:
            hostname, have_port, port = hostinfo.partition(u':')
        if not have_port:
            port = None
        return hostname, port


class _NetlocResultMixinBytes(_NetlocResultMixinBase, _ResultMixinBytes):
    __slots__ = ()

    @property
    def _userinfo(self):
        netloc = self.netloc
        userinfo, have_info, hostinfo = netloc.rpartition(b'@')
        if have_info:
            username, have_password, password = userinfo.partition(b':')
            if not have_password:
                password = None
        else:
            username = password = None
        return username, password

    @property
    def _hostinfo(self):
        netloc = self.netloc
        _, _, hostinfo = netloc.rpartition(b'@')
        _, have_open_br, bracketed = hostinfo.partition(b'[')
        if have_open_br:
            hostname, _, port = bracketed.partition(b']')
            _, have_port, port = port.partition(b':')
        else:
            hostname, have_port, port = hostinfo.partition(b':')
        if not have_port:
            port = None
        return hostname, port


from collections import namedtuple

_DefragResultBase = namedtuple('DefragResult', 'url fragment')
_SplitResultBase = namedtuple('SplitResult', 'scheme netloc path query fragment')
_ParseResultBase = namedtuple('ParseResult', 'scheme netloc path params query fragment')

# For backwards compatibility, alias _NetlocResultMixinStr
# ResultBase is no longer part of the documented API, but it is
# retained since deprecating it isn't worth the hassle
ResultBase = _NetlocResultMixinStr

# Structured result objects for string data
class DefragResult(_DefragResultBase, _ResultMixinStr):
    __slots__ = ()
    def geturl(self):
        if self.fragment:
            return self.url + u'#' + self.fragment
        else:
            return self.url

class SplitResult(_SplitResultBase, _NetlocResultMixinStr):
    __slots__ = ()
    def geturl(self):
        return urlunsplit(self)

class ParseResult(_ParseResultBase, _NetlocResultMixinStr):
    __slots__ = ()
    def geturl(self):
        return urlunparse(self)

# Structured result objects for bytes data
class DefragResultBytes(_DefragResultBase, _ResultMixinBytes):
    __slots__ = ()
    def geturl(self):
        if self.fragment:
            return self.url + b'#' + self.fragment
        else:
            return self.url

class SplitResultBytes(_SplitResultBase, _NetlocResultMixinBytes):
    __slots__ = ()
    def geturl(self):
        return urlunsplit(self)

class ParseResultBytes(_ParseResultBase, _NetlocResultMixinBytes):
    __slots__ = ()
    def geturl(self):
        return urlunparse(self)

# Set up the encode/decode result pairs
def _fix_result_transcoding():
    _result_pairs = (
        (DefragResult, DefragResultBytes),
        (SplitResult, SplitResultBytes),
        (ParseResult, ParseResultBytes),
    )
    for _decoded, _encoded in _result_pairs:
        _decoded._encoded_counterpart = _encoded
        _encoded._decoded_counterpart = _decoded

_fix_result_transcoding()
del _fix_result_transcoding

def urlparse(url, scheme=u'', allow_fragments=True):
    """Parse a URL into 6 components:
    <scheme>://<netloc>/<path>;<params>?<query>#<fragment>
    Return a 6-tuple: (scheme, netloc, path, params, query, fragment).
    Note that we don't break the components up in smaller bits
    (e.g. netloc is a single string) and we don't expand % escapes."""
    url, scheme, _coerce_result = _coerce_args(url, scheme)
    splitresult = urlsplit(url, scheme, allow_fragments)
    scheme, netloc, url, query, fragment = splitresult
    if scheme in uses_params and u';' in url:
        url, params = _splitparams(url)
    else:
        params = u''
    result = ParseResult(scheme, netloc, url, params, query, fragment)
    return _coerce_result(result)

def _splitparams(url):
    if u'/'  in url:
        i = url.find(u';', url.rfind(u'/'))
        if i < 0:
            return url, u''
    else:
        i = url.find(u';')
    return url[:i], url[i+1:]

def _splitnetloc(url, start=0):
    delim = len(url)   # position of end of domain part of url, default is end
    for c in u'/?#':    # look for delimiters; the order is NOT important
        wdelim = url.find(c, start)        # find first of this delim
        if wdelim >= 0:                    # if found
            delim = min(delim, wdelim)     # use earliest delim position
    return url[start:delim], url[delim:]   # return (domain, rest)

def urlsplit(url, scheme=u'', allow_fragments=True):
    """Parse a URL into 5 components:
    <scheme>://<netloc>/<path>?<query>#<fragment>
    Return a 5-tuple: (scheme, netloc, path, query, fragment).
    Note that we don't break the components up in smaller bits
    (e.g. netloc is a single string) and we don't expand % escapes."""
    url, scheme, _coerce_result = _coerce_args(url, scheme)
    allow_fragments = bool(allow_fragments)
    key = url, scheme, allow_fragments, type(url), type(scheme)
    cached = _parse_cache.get(key, None)
    if cached:
        return _coerce_result(cached)
    if len(_parse_cache) >= MAX_CACHE_SIZE: # avoid runaway growth
        clear_cache()
    netloc = query = fragment = u''
    i = url.find(u':')
    if i > 0:
        if url[:i] == u'http': # optimize the common case
            scheme = url[:i].lower()
            url = url[i+1:]
            if url[:2] == u'//':
                netloc, url = _splitnetloc(url, 2)
                if ((u'[' in netloc and u']' not in netloc) or
                        (u']' in netloc and u'[' not in netloc)):
                    raise ValueError("Invalid IPv6 URL")
            if allow_fragments and u'#' in url:
                url, fragment = url.split(u'#', 2)
            if u'?' in url:
                url, query = url.split(u'?', 1)
            v = SplitResult(scheme, netloc, url, query, fragment)
            _parse_cache[key] = v
            return _coerce_result(v)
        for c in url[:i]:
            if c not in scheme_chars:
                break
        else:
            # make sure "url" is not actually a port number (in which case
            # "scheme" is really part of the path)
            rest = url[i+1:]
            if not rest or any(c not in u'0123456789' for c in rest):
                # not a port number
                scheme, url = url[:i].lower(), rest

    if url[:2] == u'//':
        netloc, url = _splitnetloc(url, 2)
        if ((u'[' in netloc and u']' not in netloc) or
                (u']' in netloc and u'[' not in netloc)):
            raise ValueError("Invalid IPv6 URL")
    if allow_fragments and u'#' in url:
        url, fragment = url.split(u'#', 1)
    if u'?' in url:
        url, query = url.split(u'?', 1)
    v = SplitResult(scheme, netloc, url, query, fragment)
    _parse_cache[key] = v
    return _coerce_result(v)

def urlunparse(components):
    """Put a parsed URL back together again.  This may result in a
    slightly different, but equivalent URL, if the URL that was parsed
    originally had redundant delimiters, e.g. a ? with an empty query
    (the draft states that these are equivalent)."""
    scheme, netloc, url, params, query, fragment, _coerce_result = (
                                                  _coerce_args(*components))
    if params:
        url = u"%s;%s" % (url, params)
    return _coerce_result(urlunsplit((scheme, netloc, url, query, fragment)))

def urlunsplit(components):
    """Combine the elements of a tuple as returned by urlsplit() into a
    complete URL as a string. The data argument can be any five-item iterable.
    This may result in a slightly different, but equivalent URL, if the URL that
    was parsed originally had unnecessary delimiters (for example, a ? with an
    empty query; the RFC states that these are equivalent)."""
    scheme, netloc, url, query, fragment, _coerce_result = (
                                          _coerce_args(*components))
    if netloc or (scheme and scheme in uses_netloc and url[:2] != u'//'):
        if url and url[:1] != u'/': url = u'/' + url
        url = u'//' + (netloc or u'') + url
    if scheme:
        url = scheme + u':' + url
    if query:
        url = url + u'?' + query
    if fragment:
        url = url + u'#' + fragment
    return _coerce_result(url)

def urljoin(base, url, allow_fragments=True):
    """Join a base URL and a possibly relative URL to form an absolute
    interpretation of the latter."""
    if not base:
        return url
    if not url:
        return base
    base, url, _coerce_result = _coerce_args(base, url)
    bscheme, bnetloc, bpath, bparams, bquery, bfragment = \
            urlparse(base, u'', allow_fragments)
    scheme, netloc, path, params, query, fragment = \
            urlparse(url, bscheme, allow_fragments)
    if scheme != bscheme or scheme not in uses_relative:
        return _coerce_result(url)
    if scheme in uses_netloc:
        if netloc:
            return _coerce_result(urlunparse((scheme, netloc, path,
                                              params, query, fragment)))
        netloc = bnetloc
    if path[:1] == u'/':
        return _coerce_result(urlunparse((scheme, netloc, path,
                                          params, query, fragment)))
    if not path and not params:
        path = bpath
        params = bparams
        if not query:
            query = bquery
        return _coerce_result(urlunparse((scheme, netloc, path,
                                          params, query, fragment)))
    segments = bpath.split(u'/')[:-1] + path.split(u'/')
    # XXX The stuff below is bogus in various ways...
    if segments[-1] == u'.':
        segments[-1] = u''
    while '.' in segments:
        segments.remove(u'.')
    while 1:
        i = 1
        n = len(segments) - 1
        while i < n:
            if (segments[i] == u'..'
                and segments[i-1] not in (u'', u'..')):
                del segments[i-1:i+1]
                break
            i = i+1
        else:
            break
    if segments == [u'', u'..']:
        segments[-1] = u''
    elif len(segments) >= 2 and segments[-1] == u'..':
        segments[-2:] = [u'']
    return _coerce_result(urlunparse((scheme, netloc, u'/'.join(segments),
                                      params, query, fragment)))

def urldefrag(url):
    """Removes any existing fragment from URL.

    Returns a tuple of the defragmented URL and the fragment.  If
    the URL contained no fragments, the second element is the
    empty string.
    """
    url, _coerce_result = _coerce_args(url)
    if u'#' in url:
        s, n, p, a, q, frag = urlparse(url)
        defrag = urlunparse((s, n, p, a, q, u''))
    else:
        frag = u''
        defrag = url
    return _coerce_result(DefragResult(defrag, frag))


_hexdig = u'0123456789ABCDEFabcdef'
_hextobyte = dict(((a + b).encode(), six.int2byte(int(a + b, 16)))
                  for a in _hexdig for b in _hexdig)

def unquote_to_bytes(string, unsafe=b''):
    """unquote_to_bytes('abc%20def') -> b'abc def'."""
    # Note: strings are encoded as UTF-8. This is only an issue if it contains
    # unescaped non-ASCII characters, which URIs should not.
    assert isinstance(unsafe, six.binary_type), unsafe
    if not string:
        # Is it a string-like object?
        string.split
        return b''
    if isinstance(string, six.text_type):
        string = string.encode('ascii')
    bits = string.split(b'%')
    if len(bits) == 1:
        return string
    res = [bits[0]]
    append = res.append
    for item in bits[1:]:
        try:
            byte = _hextobyte[item[:2]]
            if byte in unsafe:
                raise KeyError()
            append(byte)
            append(item[2:])
        except KeyError:
            append(b'%')
            append(item)
    return b''.join(res)

_asciire = re.compile('([\x00-\x7f]+)')

def unquote(string, unsafe=b'', encoding='utf-8', errors='replace'):
    """Replace %xx escapes by their single-character equivalent. The optional
    encoding and errors parameters specify how to decode percent-encoded
    sequences into Unicode characters, as accepted by the bytes.decode()
    method.
    By default, percent-encoded sequences are decoded with UTF-8, and invalid
    sequences are replaced by a placeholder character.

    unquote('abc%20def') -> b'abc def'.
    """
    if not isinstance(string, six.text_type):
        string = string.decode('ascii')
    if u'%' not in string:
        string.split
        return string
    if encoding is None:
        encoding = 'utf-8'
    if errors is None:
        errors = 'replace'
    bits = _asciire.split(string)
    res = [bits[0]]
    append = res.append
    for i in range(1, len(bits), 2):
        append(unquote_to_bytes(bits[i], unsafe=unsafe).decode(encoding, errors))
        append(bits[i + 1])
    return u''.join(res)

def parse_qs(qs, keep_blank_values=False, strict_parsing=False,
             encoding='utf-8', errors='replace'):
    """Parse a query given as a string argument.

        Arguments:

        qs: percent-encoded query string to be parsed

        keep_blank_values: flag indicating whether blank values in
            percent-encoded queries should be treated as blank strings.
            A true value indicates that blanks should be retained as
            blank strings.  The default false value indicates that
            blank values are to be ignored and treated as if they were
            not included.

        strict_parsing: flag indicating what to do with parsing errors.
            If false (the default), errors are silently ignored.
            If true, errors raise a ValueError exception.

        encoding and errors: specify how to decode percent-encoded sequences
            into Unicode characters, as accepted by the bytes.decode() method.
    """
    parsed_result = {}
    pairs = parse_qsl(qs, keep_blank_values, strict_parsing,
                      encoding=encoding, errors=errors)
    for name, value in pairs:
        if name in parsed_result:
            parsed_result[name].append(value)
        else:
            parsed_result[name] = [value]
    return parsed_result

def parse_qsl(qs, keep_blank_values=False, strict_parsing=False,
              encoding='utf-8', errors='replace'):
    """Parse a query given as a string argument.

    Arguments:

    qs: percent-encoded query string to be parsed

    keep_blank_values: flag indicating whether blank values in
        percent-encoded queries should be treated as blank strings.  A
        true value indicates that blanks should be retained as blank
        strings.  The default false value indicates that blank values
        are to be ignored and treated as if they were  not included.

    strict_parsing: flag indicating what to do with parsing errors. If
        false (the default), errors are silently ignored. If true,
        errors raise a ValueError exception.

    encoding and errors: specify how to decode percent-encoded sequences
        into Unicode characters, as accepted by the bytes.decode() method.

    Returns a list, as G-d intended.
    """
    qs, _coerce_result = _coerce_args(qs)
    pairs = [s2 for s1 in qs.split(u'&') for s2 in s1.split(u';')]
    r = []
    for name_value in pairs:
        if not name_value and not strict_parsing:
            continue
        nv = name_value.split(u'=', 1)
        if len(nv) != 2:
            if strict_parsing:
                raise ValueError("bad query field: %r" % (name_value,))
            # Handle case of a control-name with no equal sign
            if keep_blank_values:
                nv.append(u'')
            else:
                continue
        if len(nv[1]) or keep_blank_values:
            name = nv[0].replace(u'+', u' ')
            name = unquote(name, encoding=encoding, errors=errors)
            name = _coerce_result(name)
            value = nv[1].replace(u'+', u' ')
            value = unquote(value, encoding=encoding, errors=errors)
            value = _coerce_result(value)
            r.append((name, value))
    return r

def unquote_plus(string, encoding='utf-8', errors='replace'):
    """Like unquote(), but also replace plus signs by spaces, as required for
    unquoting HTML form values.

    unquote_plus('%7e/abc+def') -> '~/abc def'
    """
    string = string.replace(u'+', u' ')
    return unquote(string, encoding=encoding, errors=errors)

def _iter_bytestring(string):
    #XXX: replace this with the new six feature
    if six.PY3 or isinstance(string, bytearray):
        return iter(string)
    return iter(map(ord, string))

_ALWAYS_SAFE_BYTES = (b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                      b'abcdefghijklmnopqrstuvwxyz'
                      b'0123456789'
                      b'_.-+')  # + maybe shouldn't be here
_ALWAYS_SAFE = frozenset(_iter_bytestring(_ALWAYS_SAFE_BYTES))
_safe_quoters = {}

class Quoter(dict):
    """A mapping from bytes (in range(0,256)) to unicode strings.

    String values are percent-encoded byte values, unless the key < 128, and
    in the "safe" set (either the specified safe set, or default set).
    """
    # Keeps a cache internally, using defaultdict, for efficiency (lookups
    # of cached keys don't call Python code at all).
    def __init__(self, safe):
        """safe: bytes object."""
        self.safe = _ALWAYS_SAFE.union(_iter_bytestring(safe))

    def __repr__(self):
        # Without this, will just display as a defaultdict
        return "<Quoter %r>" % dict.__repr__(self)

    def __missing__(self, b):
        # Handle a cache miss. Store quoted string in cache and return.
        res = six.int2byte(b).decode() if b in self.safe else u'%{:02X}'.format(b)
        self[b] = res
        return res

def quote(string, safe='/', encoding=None, errors=None):
    """quote('abc def') -> u'abc%20def'

    Each part of a URL, e.g. the path info, the query, etc., has a
    different set of reserved characters that must be quoted.

    RFC 2396 Uniform Resource Identifiers (URI): Generic Syntax lists
    the following reserved characters.

    reserved    = ";" | "/" | "?" | ":" | "@" | "&" | "=" | "+" |
                  "$" | ","

    Each of these characters is reserved in some component of a URL,
    but not necessarily in all of them.

    By default, the quote function is intended for quoting the path
    section of a URL.  Thus, it will not encode '/'.  This character
    is reserved, but in typical usage the quote function is being
    called on a path where the existing slash characters are used as
    reserved characters.

    string and safe may be either str or bytes objects. encoding must
    not be specified if string is a str.

    The optional encoding and errors parameters specify how to deal with
    non-ASCII characters, as accepted by the str.encode method.
    By default, encoding='utf-8' (characters are encoded with UTF-8), and
    errors='strict' (unsupported characters raise a UnicodeEncodeError).
    """
    if isinstance(string, six.text_type):
        if not string:
            return string
        if encoding is None:
            encoding = 'utf-8'
        if errors is None:
            errors = 'strict'
        string = string.encode(encoding, errors)
    else:
        if encoding is not None:
            raise TypeError("quote() doesn't support 'encoding' for bytes")
        if errors is not None:
            raise TypeError("quote() doesn't support 'errors' for bytes")
    return quote_from_bytes(string, safe)

def quote_plus(string, safe='', encoding=None, errors=None):
    """Like quote(), but also replace ' ' with '+', as required for quoting
    HTML form values. Plus signs in the original string are escaped unless
    they are included in safe. It also does not have safe default to '/'.
    """
    # Check if ' ' in string, where string may either be a str or bytes.  If
    # there are no spaces, the regular quote will produce the right answer.
    if ((isinstance(string, six.text_type) and u' ' not in string) or
        (isinstance(string, six.binary_type) and b' ' not in string)):
        return quote(string, safe, encoding, errors)
    if isinstance(safe, six.text_type):
        space = u' '
    else:
        space = b' '
    string = quote(string, safe=(safe + space), encoding=encoding, errors=errors)
    return string.replace(u' ', u'+')

def quote_from_bytes(bs, safe='/'):
    """Like quote(), but accepts a bytes object rather than a str, and does
    not perform string-to-bytes encoding.  It always returns an ASCII string.
    quote_from_bytes(b'abc def\x3f') -> 'abc%20def%3f'
    """
    if not isinstance(bs, (bytes, bytearray)):
        raise TypeError("quote_from_bytes() expected bytes")
    if not bs:
        return u''
    if isinstance(safe, six.text_type):
        # Normalize 'safe' by converting to bytes and removing non-ASCII chars
        safe = safe.encode('ascii', 'ignore')
    else:
        safe = b''.join(six.int2byte(c) for c in _iter_bytestring(safe) if c < 128)
    if not bs.rstrip(_ALWAYS_SAFE_BYTES + safe):
        return bs.decode()
    try:
        quoter = _safe_quoters[safe]
    except KeyError:
        _safe_quoters[safe] = quoter = Quoter(safe).__getitem__
    return u''.join([quoter(char) for char in _iter_bytestring(bs)])

def urlencode(query, doseq=False, safe=u'', encoding=None, errors=None):
    """Encode a sequence of two-element tuples or dictionary into a URL query string.

    If any values in the query arg are sequences and doseq is true, each
    sequence element is converted to a separate parameter.

    If the query arg is a sequence of two-element tuples, the order of the
    parameters in the output will match the order of parameters in the
    input.

    The query arg may be either a string or a bytes type. When query arg is a
    string, the safe, encoding and error parameters are sent the quote_plus for
    encoding.
    """

    if hasattr(query, "items"):
        query = six.iteritems(query)
    else:
        # It's a bother at times that strings and string-like objects are
        # sequences.
        try:
            # non-sequence items should not work with len()
            # non-empty strings will fail this
            if len(query) and not isinstance(query[0], tuple):
                raise TypeError
            # Zero-length sequences of all types will get here and succeed,
            # but that's a minor nit.  Since the original implementation
            # allowed empty dicts that type of behavior probably should be
            # preserved for consistency
        except TypeError:
            ty, va, tb = sys.exc_info()
            va = TypeError("not a valid non-string sequence or mapping object")
            six.reraise(ty, va, tb)

    l = []
    if not doseq:
        for k, v in query:
            if isinstance(k, six.binary_type):
                k = quote_plus(k, safe)
            else:
                k = quote_plus(six.text_type(k), safe, encoding, errors)

            if isinstance(v, six.binary_type):
                v = quote_plus(v, safe)
            else:
                v = quote_plus(six.text_type(v), safe, encoding, errors)
            l.append(k + u'=' + v)
    else:
        for k, v in query:
            if isinstance(k, six.binary_type):
                k = quote_plus(k, safe)
            else:
                k = quote_plus(six.text_type(k), safe, encoding, errors)

            if isinstance(v, six.binary_type):
                v = quote_plus(v, safe)
                l.append(k + u'=' + v)
            elif isinstance(v, six.text_type):
                v = quote_plus(v, safe, encoding, errors)
                l.append(k + u'=' + v)
            else:
                try:
                    # Is this a sufficient test for sequence-ness?
                    x = len(v)
                except TypeError:
                    # not a sequence
                    v = quote_plus(six.text_type(v), safe, encoding, errors)
                    l.append(k + u'=' + v)
                else:
                    # loop over the sequence
                    for elt in v:
                        if isinstance(elt, six.binary_type):
                            elt = quote_plus(elt, safe)
                        else:
                            elt = quote_plus(six.text_type(elt), safe, encoding, errors)
                        l.append(k + u'=' + elt)
    return u'&'.join(l)

# Utilities to parse URLs (most of these return None for missing parts):
# unwrap('<URL:type://host/path>') --> 'type://host/path'
# splittype('type:opaquestring') --> 'type', 'opaquestring'
# splithost('//host[:port]/path') --> 'host[:port]', '/path'
# splituser('user[:passwd]@host[:port]') --> 'user[:passwd]', 'host[:port]'
# splitpasswd('user:passwd') -> 'user', 'passwd'
# splitport('host:port') --> 'host', 'port'
# splitquery('/path?query') --> '/path', 'query'
# splittag('/path#tag') --> '/path', 'tag'
# splitattr('/path;attr1=value1;attr2=value2;...') ->
#   '/path', ['attr1=value1', 'attr2=value2', ...]
# splitvalue('attr=value') --> 'attr', 'value'
# urllib.parse.unquote('abc%20def') -> 'abc def'
# quote('abc def') -> 'abc%20def')

def to_bytes(url):
    """to_bytes(u"URL") --> 'URL'."""
    # Most URL schemes require ASCII. If that changes, the conversion
    # can be relaxed.
    # XXX get rid of to_bytes()
    if isinstance(url, six.text_type):
        try:
            url = url.encode("ASCII").decode()
        except UnicodeError:
            raise UnicodeError("URL " + repr(url) +
                               " contains non-ASCII characters")
    return url

def unwrap(url):
    """unwrap('<URL:type://host/path>') --> 'type://host/path'."""
    url = str(url).strip()
    if url[:1] == u'<' and url[-1:] == u'>':
        url = url[1:-1].strip()
    if url[:4] == u'URL:':
        url = url[4:].strip()
    return url

_typeprog = None
def splittype(url):
    """splittype('type:opaquestring') --> 'type', 'opaquestring'."""
    global _typeprog
    if _typeprog is None:
        import re
        _typeprog = re.compile('^([^/:]+):')

    match = _typeprog.match(url)
    if match:
        scheme = match.group(1)
        return scheme.lower(), url[len(scheme) + 1:]
    return None, url

_hostprog = None
def splithost(url):
    """splithost('//host[:port]/path') --> 'host[:port]', '/path'."""
    global _hostprog
    if _hostprog is None:
        import re
        _hostprog = re.compile('^//([^/?]*)(.*)$')

    match = _hostprog.match(url)
    if match:
        host_port = match.group(1)
        path = match.group(2)
        if path and not path.startswith(u'/'):
            path = u'/' + path
        return host_port, path
    return None, url

_userprog = None
def splituser(host):
    """splituser('user[:passwd]@host[:port]') --> 'user[:passwd]', 'host[:port]'."""
    global _userprog
    if _userprog is None:
        import re
        _userprog = re.compile('^(.*)@(.*)$')

    match = _userprog.match(host)
    if match: return match.group(1, 2)
    return None, host

_passwdprog = None
def splitpasswd(user):
    """splitpasswd('user:passwd') -> 'user', 'passwd'."""
    global _passwdprog
    if _passwdprog is None:
        import re
        _passwdprog = re.compile('^([^:]*):(.*)$',re.S)

    match = _passwdprog.match(user)
    if match: return match.group(1, 2)
    return user, None

# splittag('/path#tag') --> '/path', 'tag'
_portprog = None
def splitport(host):
    """splitport('host:port') --> 'host', 'port'."""
    global _portprog
    if _portprog is None:
        import re
        _portprog = re.compile('^(.*):([0-9]+)$')

    match = _portprog.match(host)
    if match: return match.group(1, 2)
    return host, None

_nportprog = None
def splitnport(host, defport=-1):
    """Split host and port, returning numeric port.
    Return given default port if no ':' found; defaults to -1.
    Return numerical port if a valid number are found after ':'.
    Return None if ':' but not a valid number."""
    global _nportprog
    if _nportprog is None:
        import re
        _nportprog = re.compile('^(.*):(.*)$')

    match = _nportprog.match(host)
    if match:
        host, port = match.group(1, 2)
        try:
            if not port: raise ValueError("no digits")
            nport = int(port)
        except ValueError:
            nport = None
        return host, nport
    return host, defport

_queryprog = None
def splitquery(url):
    """splitquery('/path?query') --> '/path', 'query'."""
    global _queryprog
    if _queryprog is None:
        import re
        _queryprog = re.compile('^(.*)\?([^?]*)$')

    match = _queryprog.match(url)
    if match:
        return match.group(1, 2)
    return url, None

_tagprog = None
def splittag(url):
    """splittag('/path#tag') --> '/path', 'tag'."""
    global _tagprog
    if _tagprog is None:
        import re
        _tagprog = re.compile('^(.*)#([^#]*)$')

    match = _tagprog.match(url)
    if match:
        return match.group(1, 2)
    return url, None

def splitattr(url):
    """splitattr('/path;attr1=value1;attr2=value2;...') ->
        '/path', ['attr1=value1', 'attr2=value2', ...]."""
    words = url.split(u';')
    return words[0], words[1:]

_valueprog = None
def splitvalue(attr):
    """splitvalue('attr=value') --> 'attr', 'value'."""
    global _valueprog
    if _valueprog is None:
        import re
        _valueprog = re.compile('^([^=]*)=(.*)$')

    match = _valueprog.match(attr)
    if match: return match.group(1, 2)
    return attr, None