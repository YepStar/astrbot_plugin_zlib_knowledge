"""Provider metadata to readable plain text; preserve paragraphs, not inline-tag breaks."""
import re
from bs4 import BeautifulSoup


def plain_description(value):
    soup=BeautifulSoup(str(value or ''),'html.parser')
    for element in soup(['script','style']):element.decompose()
    separator='\ue000'
    for br in soup.find_all('br'):br.replace_with(separator)
    for block in soup.find_all(['p','div','li','h1','h2','h3','h4']):block.append(separator)
    text=' '.join(soup.get_text(' ',strip=True).split())
    return re.sub(r'(?: *'+separator+r' *)+','\n\n',text).strip()
