import pytest
import yaml

from paperops_mcp import config as cfgmod
from paperops_mcp import http
from paperops_mcp.core import open_ctx

KEY_ENVS = ("NCBI_API_KEY", "OPENALEX_API_KEY", "S2_API_KEY", "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN",
            "WILEY_TDM_TOKEN", "SPRINGER_API_KEY", "PAPEROPS_EMAIL")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"data_root": str(tmp_path / "data"), "state_dir": str(tmp_path / "state"),
                                    "http": {"retries": 0, "timeout": 5}}), encoding="utf-8")
    monkeypatch.setenv("PAPEROPS_CONFIG", str(path))
    for name in KEY_ENVS:  # unit tests must not depend on (or leak) the developer's keys
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(http, "MIN_INTERVAL", {})  # no politeness delays against mocked hosts
    monkeypatch.setattr(http, "_resolve", lambda host: set())  # no real DNS lookups in unit tests
    cfgmod.clear_cache()
    yield cfgmod.load_config(use_cache=False)
    cfgmod.clear_cache()


@pytest.fixture
def ctx(cfg):
    with open_ctx(cfg) as c:
        yield c


def make_pdf(text: str = "The repertoire of mutational signatures in human cancer. " * 20) -> bytes:
    """A minimal one-page PDF with real, extractable text (offsets computed, so pypdf reads it cleanly)."""
    stream = f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


JATS = """<?xml version="1.0"?>
<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD v1.2 20190208//EN" "JATS-archivearticle1.dtd">
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<title-group><article-title>The repertoire of mutational signatures in human cancer</article-title></title-group>
<abstract><p>Somatic mutations are caused by mutational processes.</p></abstract></article-meta></front>
<body><sec><title>Results</title><p>%s</p>
<sec><title>Signatures</title><p>SBS1 and SBS5 are clock-like.</p></sec>
<supplementary-material><media xlink:href="41586_2020_1943_MOESM1_ESM.pdf"/></supplementary-material>
</sec></body></article>""" % ("We analysed 4,645 whole genomes. " * 80)

PUBMED_XML = """<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation>
<PMID Version="1">32025018</PMID><Article><Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
<Title>Nature</Title><ISOAbbreviation>Nature</ISOAbbreviation></Journal>
<ArticleTitle>The repertoire of <i>mutational</i> signatures in human cancer.</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">Somatic mutations.</AbstractText><AbstractText>More text.</AbstractText></Abstract>
<AuthorList><Author><LastName>Alexandrov</LastName><Initials>LB</Initials></Author>
<Author><CollectiveName>PCAWG Consortium</CollectiveName></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article>
<MeshHeadingList><MeshHeading><DescriptorName>Mutation</DescriptorName></MeshHeading></MeshHeadingList>
</MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">32025018</ArticleId>
<ArticleId IdType="doi">10.1038/s41586-020-1943-3</ArticleId><ArticleId IdType="pmc">PMC7054213</ArticleId>
</ArticleIdList><ReferenceList><Reference><ArticleIdList><ArticleId IdType="doi">10.1/other</ArticleId>
</ArticleIdList></Reference></ReferenceList></PubmedData></PubmedArticle></PubmedArticleSet>"""
