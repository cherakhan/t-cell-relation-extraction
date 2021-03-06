from bs4 import BeautifulSoup
import pandas as pd
import re
import string
import sys
import unicodedata
from collections import defaultdict
import logging
logger = logging.getLogger(__name__)

CHR_CAT_MAP = defaultdict(list)
for c in map(chr, range(sys.maxunicode + 1)):
    CHR_CAT_MAP[unicodedata.category(c)].append(c)

WS_REGEX = re.compile('\n{2,}')

# All whitespace or dash characters of any kind (https://www.fileformat.info/info/unicode/category/Pd/list.htm)
ENC_CHRS = '\\s,\\' + '\\'.join(CHR_CAT_MAP['Pd']) 
ENC_REGEX_1 = re.compile(r'\([' + ENC_CHRS + r']*?\)')
ENC_REGEX_2 = re.compile(r'\[[' + ENC_CHRS + r']*?\]')
PNC_REGEX_1 = re.compile(r'\s+(\.|\?|\!)')


def clean_text_whitespace(text):
    # Remove individual lines that have a very small number of characters
    text = '\n'.join([l for l in text.split('\n') if len(l.strip()) == 0 or len(l.strip()) >= 64])
    # Replace 2+ newlines 
    text = WS_REGEX.sub('\n\n', text)
    return text


def clean_text_enclosures(text):
    # Remove empty enclosures like "(\t)" or "[]"
    text = ENC_REGEX_1.sub('', text)
    text = ENC_REGEX_2.sub('', text)
    return text


def clean_text_punctuation(text):
    # Remove whitespace before punctuation
    text = PNC_REGEX_1.sub('\\1', text)
    return text


def combine_text(title, abstract, body):
    parts = ['' if pd.isnull(p) else p for p in [title, abstract, body]]
    
    def add_punc(t):
        return t + '.' if t.strip() and t.strip()[-1] not in string.punctuation else t
    
    return '\n'.join([add_punc(p) for p in parts])


def parse_article(soup, clean_text=True):
    res = {}
    
    # Extract IDs
    ids = soup.find('article-meta')
    ids = ids.find_all('article-id') if ids else []
    
    def get_id(typ):
        idt = [t for t in ids if t.get('pub-id-type') == typ]
        return idt[0].text if idt else None
    res['id_pmc'] = get_id('pmc')
    res['id_pmid'] = get_id('pmid')
    res['id_doi'] = get_id('doi')
        
    # Extract dates
    def parse_date(t):
        if not t or not t.find('year'):
            return None
        date_string = '{}-{}-{}'.format(
            t.find('year').text,
            t.find('month').text if t.find('month') else '01',
            t.find('day').text if t.find('day') else '01'
        )
        try:
            return pd.to_datetime(date_string)
        except Exception as e:
            logger.warning('Failed to parse date string "%s"; Reason: %s', date_string, e)
            return None
    
    # Dates related to transmission
    history_dates = soup.find('history')
    history_dates = history_dates.find_all('date') if history_dates else []

    def get_history_date(typ):
        dt = [t for t in history_dates if t.get('date-type') == typ]
        return parse_date(dt[0]) if dt else None
    res['date_received'] = get_history_date('received')
    res['date_accepted'] = get_history_date('accepted')
    
    # Earlist publication date
    pub_dates = [parse_date(t) for t in soup.find_all('pub-date')]
    pub_dates = [date for date in pub_dates if date is not None]
    res['date_pub'] = min(pub_dates) if pub_dates else None
    
    # Extract journal metadata
    res['journal_titles'] = '|:|'.join(list(set([t.text for t in soup.find_all('journal-title')])))
    res['journal_ids'] = '|:|'.join(list(set([t.text for t in soup.find_all('journal-id')])))

    # Extract textual components
    def extract_text(t):
        if t is None:
            return None
        if not clean_text:
            return t.text
        
        # First remove all citations
        for citet in t.find_all('xref'):
            citet.replace_with('')
        text = t.text
        text = clean_text_enclosures(text)
        text = clean_text_whitespace(text)
        text = clean_text_punctuation(text)
        return text.strip()
            
    try:
        res['title'] = extract_text(soup.find('title-group').find('article-title'))
    except:
        res['title'] = None
            
    try:
        res['abstract'] = extract_text(soup.find('abstract'))
    except:
        res['abstract'] = None
        
    try:
        res['body'] = extract_text(soup.find('body'))
    except:
        res['body'] = None
    return res


def parse_nxml(doc):
    soup = BeautifulSoup(doc, 'xml')
    return pd.DataFrame([parse_article(article) for article in soup.find_all('article')])


def extract_corpus(stream, output_file, batch_size=1000, parser_fn=parse_nxml):
    import pyarrow.parquet as pq
    import pyarrow as pa
    dfs = []
    writer = None

    def flush(dfs, writer):
        dfs = pd.concat(dfs)
        table = pa.Table.from_pandas(dfs, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(output_file, table.schema)
        writer.write_table(table)
        return writer

    for row, text in stream:
        df = parser_fn(text)
        if row is not None:
            df = df.assign(**{k: v for k, v in row.items()})

        # Convert to string to avoid issue with all null vs datetime type fields
        for col in df.filter(regex='date_'):
            df[col] = df[col].astype(str)

        if len(df) > 0:
            dfs.append(df)
        if len(dfs) >= batch_size:
            writer = flush(dfs, writer)
            dfs = []
    if len(dfs) > 0:
        writer = flush(dfs, writer)
    if writer is not None:
        writer.close()


def get_scispacy_pipeline(model='en_ner_jnlpba_md'):
    import spacy
    # Scispacy post-release
    nlp = spacy.load('en_core_sci_md')
    # en_ner_jnlpba_md or en_ner_craft_md are most appropriate
    ner = spacy.load(model)
    nlp.replace_pipe('ner', ner.pipeline[0][1])
    
    # The individual entity type names (e.g. CELL_TYPE, PROTEIN, etc. need to be 
    # added to the core nlp vocab in order to avoid "label not in StringStore errors")
    ner_types = sorted(list(set([typ.split('-')[-1] for typ in ner.pipeline[0][1].move_names])))
    for typ in ner_types:
        nlp.vocab.strings.add(typ)
        
    return nlp
