"""Closed formatted source projection and complete escaped evidence.

Entity semantics use the pinned TDLib UTF-16 coordinate basis. Display sanitation
never supplies coordinates for validation or changes the raw source identity.
"""
from __future__ import annotations

import json
import unicodedata

from .sanitize import sanitize_telegram_text

OFFSET_BASIS = 'original source UTF-16 code units'
STYLES = {
    'textEntityTypeBold':'bold', 'textEntityTypeItalic':'italic',
    'textEntityTypeUnderline':'underline', 'textEntityTypeStrikethrough':'strikethrough',
    'textEntityTypeSpoiler':'spoiler', 'textEntityTypeCode':'code',
    'textEntityTypePre':'pre', 'textEntityTypePreCode':'pre_code',
    'textEntityTypeBlockQuote':'block_quote',
    'textEntityTypeExpandableBlockQuote':'expandable_block_quote',
}
LEXICAL = {
    'textEntityTypeHashtag':'hashtag', 'textEntityTypeCashtag':'cashtag',
    'textEntityTypeBotCommand':'bot_command',
}
IDENTITY = {
    'textEntityTypeMention':'mention', 'textEntityTypeMentionName':'mention_name',
}
DATETIME = {'textEntityTypeDateTime':'date_time'}
_CODE = frozenset(('textEntityTypeCode','textEntityTypePre','textEntityTypePreCode'))
_QUOTES = frozenset(('textEntityTypeBlockQuote','textEntityTypeExpandableBlockQuote'))


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _text(value, *, language=False):
    if type(value) is not str:
        raise ValueError('invalid formatted reply source')
    if language:
        if len(value)>64 or len(value.encode('utf-8'))>128:
            raise ValueError('invalid formatted reply language')
    elif not 0<len(value)<=4096 or not value.strip():
        raise ValueError('invalid formatted reply source')
    if any(unicodedata.category(c).startswith('C') and (language or c not in '\n\t') for c in value):
        raise ValueError('invalid formatted reply source')
    value.encode('utf-8')
    return value


def _boundaries(text):
    """Map original UTF-16 boundaries to Python scalar indices, without normalization."""
    boundaries={0:0};units=0
    for index,char in enumerate(text):
        units+=2 if ord(char)>0xffff else 1
        boundaries[units]=index+1
    return boundaries


def formatted_text(value, *, lexical=False, identity=False, datetime=False):
    """Validate exact TDLib entities and return a fresh canonically ordered shape."""
    if (type(value) is not dict or set(value)!={'@type','text','entities'} or
            value['@type']!='formattedText' or type(value['entities']) is not list or
            not 1<=len(value['entities'])<=32):
        raise ValueError('invalid formatted reply source')
    allowed={**STYLES,**(LEXICAL if lexical else {}),**(IDENTITY if identity else {}),
        **(DATETIME if datetime else {})}
    text=_text(value['text']);boundaries=_boundaries(text);entities=[];seen=set()
    for entity in value['entities']:
        if type(entity) is not dict or set(entity)!={'@type','offset','length','type'} or entity['@type']!='textEntity':
            raise ValueError('invalid formatted reply entity')
        offset=entity['offset'];length=entity['length'];typ=entity['type']
        if (type(offset) is not int or type(length) is not int or
                not 0<=offset<2**31 or not 1<=length<2**31 or
                offset not in boundaries or offset+length not in boundaries or
                type(typ) is not dict or type(typ.get('@type')) is not str or typ['@type'] not in allowed):
            raise ValueError('invalid formatted reply entity')
        kind=typ['@type'];language='';user_id=None;unix_time=None
        keys={'@type','language'} if kind=='textEntityTypePreCode' else {'@type','user_id'} if kind=='textEntityTypeMentionName' else {'@type'}
        if kind in DATETIME:
            if not {'@type','unix_time'}<=set(typ)<={'@type','unix_time','formatting_type'}:
                raise ValueError('invalid formatted reply entity')
            unix_time=typ['unix_time']
            if type(unix_time) is not int or not -(2**31)<=unix_time<2**31 or typ.get('formatting_type') is not None:
                raise ValueError('invalid formatted reply date time')
        elif set(typ)!=keys:
            raise ValueError('invalid formatted reply entity')
        if kind=='textEntityTypePreCode':language=_text(typ['language'],language=True)
        if kind=='textEntityTypeMentionName':
            user_id=typ['user_id']
            if type(user_id) is not int or not 0<user_id<2**53:
                raise ValueError('invalid formatted reply identity')
        key=(offset,length,kind,language,user_id,unix_time)
        if key in seen:raise ValueError('duplicate formatted reply entity')
        seen.add(key)
        entities.append({'@type':'textEntity','offset':offset,'length':length,
            'type':{'@type':kind,**({'language':language} if kind=='textEntityTypePreCode' else {}),
                    **({'user_id':user_id} if kind=='textEntityTypeMentionName' else {}),
                    **({'unix_time':unix_time,'formatting_type':None} if kind in DATETIME else {})}})
    entities.sort(key=lambda e:(e['offset'],-e['length'],e['type']['@type'],e['type'].get('language',''),e['type'].get('user_id',0)))
    exclusive={*LEXICAL,*IDENTITY}
    for index,left in enumerate(entities):
        start=left['offset'];end=start+left['length'];kind=left['type']['@type']
        for right in entities[index+1:]:
            rstart=right['offset'];rend=rstart+right['length'];rkind=right['type']['@type']
            if rstart>=end:break
            if (kind in DATETIME or rkind in DATETIME or kind in _CODE or rkind in _CODE or (kind in _QUOTES and rkind in _QUOTES) or
                    (kind in exclusive and rkind in exclusive) or
                    (kind in exclusive and rkind in _QUOTES) or (kind in _QUOTES and rkind in exclusive)):
                raise ValueError('excluded formatted reply overlap')
            if not (start<=rstart and rend<=end):
                raise ValueError('crossing formatted reply entities')
    return {'@type':'formattedText','text':text,'entities':entities}


def validate_content(value, *, lexical=False, identity=False, datetime=False):
    if (type(value) is not dict or set(value)!={'@type','text','link_preview','link_preview_options'} or
            value['@type']!='messageText' or value['link_preview'] is not None):
        raise ValueError('invalid formatted reply content')
    # Link option validation belongs to the unchanged message shell parser.
    if formatted_text(value['text'],lexical=lexical,identity=identity,datetime=datetime)!=value['text']:
        raise ValueError('noncanonical formatted reply entities')


def render_formatted_text(formatted, *, lexical=False, identity=False, datetime=False):
    """Complete span evidence, deriving covered text before display sanitation."""
    if formatted_text(formatted,lexical=lexical,identity=identity,datetime=datetime)!=formatted:
        raise ValueError('noncanonical formatted reply entities')
    labels={**STYLES,**(LEXICAL if lexical else {}),**(IDENTITY if identity else {}),
        **(DATETIME if datetime else {})}
    text=formatted['text'];boundaries=_boundaries(text)
    safe=sanitize_telegram_text(text,max_length=65537);changed=safe!=text;entities=[]
    for entity in formatted['entities']:
        offset=entity['offset'];length=entity['length'];typ=entity['type']
        covered=text[boundaries[offset]:boundaries[offset+length]]
        span=sanitize_telegram_text(covered,max_length=65537);changed|=span!=covered
        record={'type':labels[typ['@type']],'offset':offset,'length':length,'text':span}
        if typ['@type']=='textEntityTypePreCode':
            language=sanitize_telegram_text(typ['language'],max_length=65537)
            changed|=language!=typ['language'];record['language']=language
        if typ['@type']=='textEntityTypeMentionName':record['user_id']=typ['user_id']
        if typ['@type'] in DATETIME:
            record.update(provider_type=typ['@type'],unix_time=typ['unix_time'],formatting_type=None)
        entities.append(record)
    return {'text':safe,'offset_basis':OFFSET_BASIS,'entities':entities},changed


def render_content(value, marker, *, lexical=False, identity=False, datetime=False):
    validate_content(value,lexical=lexical,identity=identity,datetime=datetime)
    record,changed=render_formatted_text(value['text'],lexical=lexical,identity=identity,datetime=datetime)
    display=marker+'Formatted target: '+_json(record)
    if len(display)>4096 or sanitize_telegram_text(display,max_length=4097)!=display:
        raise ValueError('formatted reply evidence exceeds preview bound')
    return display,changed
