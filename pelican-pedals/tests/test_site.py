import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


class SiteParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.html_attrs = {}
        self.meta_attrs = []
        self.images = []
        self.references = []
        self.in_h1 = False
        self.h1_text = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "html":
            self.html_attrs = values
        elif tag == "meta":
            self.meta_attrs.append(values)
        elif tag == "img":
            self.images.append(values)
        elif tag == "h1":
            self.in_h1 = True
        for attribute in ("src", "href"):
            if attribute in values:
                self.references.append(values[attribute])

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_h1 = False

    def handle_data(self, data):
        if self.in_h1:
            self.h1_text.append(data)


class SiteTest(unittest.TestCase):
    def setUp(self):
        index = DIST / "index.html"
        self.html = index.read_text(encoding="utf-8") if index.exists() else ""
        self.parser = SiteParser()
        self.parser.feed(self.html)
        self.svg_path = DIST / "assets" / "pelican-pedals.svg"
        self.css_path = DIST / "styles.css"

    def test_page_presents_requested_artwork_accessibly(self):
        self.assertEqual(self.parser.html_attrs.get("lang"), "zh-CN")
        self.assertEqual("".join(self.parser.h1_text).strip(), "乘风而行")
        self.assertIn("沿着海岸，把日子踩成一阵风。", self.html)
        self.assertEqual(len(self.parser.images), 1)
        image = self.parser.images[0]
        self.assertEqual(image.get("src"), "assets/pelican-pedals.svg")
        self.assertEqual(image.get("alt"), "一只白色鹈鹕在海岸边骑着蓝色自行车")
        self.assertEqual((image.get("width"), image.get("height")), ("1200", "900"))

    def test_svg_is_valid_and_contains_bicycle_and_pelican(self):
        self.assertTrue(self.svg_path.exists(), "SVG artwork is missing")
        root = ET.parse(self.svg_path).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 1200 900")
        self.assertEqual((root.get("width"), root.get("height")), ("1200", "900"))
        ids = {element.get("id") for element in root.iter()}
        self.assertIn("wheel", ids)
        self.assertIn("bicycle-frame", ids)
        self.assertIn("pelican-body", ids)
        titles = [element.text for element in root.iter() if element.tag.endswith("title")]
        descriptions = [element.text for element in root.iter() if element.tag.endswith("desc")]
        self.assertTrue(any(titles))
        self.assertTrue(any(descriptions))

    def test_responsive_stylesheet_and_local_assets_exist(self):
        self.assertTrue(self.css_path.exists(), "Stylesheet is missing")
        css = self.css_path.read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn(":root", css)
        viewports = [meta for meta in self.parser.meta_attrs if meta.get("name") == "viewport"]
        self.assertEqual(len(viewports), 1)
        self.assertIn("width=device-width", viewports[0].get("content", ""))
        for reference in self.parser.references:
            if reference.startswith("data:"):
                continue
            self.assertFalse(reference.startswith(("https://", "http://", "//")))
            self.assertTrue((DIST / reference).exists(), f"Missing local asset: {reference}")


if __name__ == "__main__":
    unittest.main()
