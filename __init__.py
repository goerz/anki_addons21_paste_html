# -*- coding: utf-8 -*-
#
# PasteHTML
#   Let you paste richtext to anki, with formatting preserved.
#
# v3. rewrite for Anki 2.1 (Michael Goerz)
# v2. bugfix, embedded images are now supported
# v1. Initial release
#

import sys
from html.parser import HTMLParser
import re
import cgi
import urllib.request, urllib.error, urllib.parse
import os

from aqt.editor import Editor, EditorWebView
from aqt.qt import (
    Qt,
    QClipboard,
    QWebEngineView,
    QDialog,
    QImage,
    QLabel,
    QVBoxLayout,
    QMimeData,
)
from anki.utils import namedtmp
from aqt.utils import tooltip
from anki.lang import _
from anki.hooks import wrap, addHook

ADDON_PATH = os.path.dirname(__file__)
ICONS_PATH = os.path.join(ADDON_PATH, "icons")

# Tags that don't have ending tags.
_voidElements = {
    'area', 'base', 'basefont', 'bgsound', 'br', 'col',
    'command', 'embed', 'frame', 'hr', 'image', 'img', 'input', 'isindex',
    'keygen', 'link', 'menuitem', 'meta', 'nextid', 'param', 'source',
    'track', 'wbr'
}


# Tags that should be allowed
_allowedTags = {
    # Paragraph-related elements
    'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'blockquote', 'pre',

    # Useful inline elements
    'img', 'a', 'span', 'br', 'code',
    'b', 'em', 'i', 'u', 'strong',

    # Lists
    'ul', 'ol', 'li',

    # Useful layout elements
    'div', 'table', 'tr', 'td', 'thead', 'th', 'tbody',
}


# Tags that should be ignored: They are valid, but shouldn't be present in
# the output html
_ignoredTags = {
    'html', 'body',
}


# Allowed attributes
_allowedAttributes = {
    'style',

    'src', 'alt',  # img
    'href', 'title',  # a href

    'colspan', 'rowspan',  # table
}


# Allowed CSS styles
_allowedStyles = {
    # General text attributes
    'font-weight', 'color', 'background-color', 'font-style',

    # Block attributes
    'text-align', 'valign',

    # Table attributes
    'background', 'background-color',
}


# CleanHTML overrides some default styles
_overrideStyles = {
    'table': {
        'box-sizing': 'border-box',
        'width': '100%',
        'margin': '.5em',
        'border-collapse': 'collapse',
        'outline': '1px solid black',
    },

    'th': {
        'position': 'relative',
        'border': '1px solid black',
        'padding': '.4em',
        'font-size': '1.2em',
    },

    'td': {
        'position': 'relative',
        'border': '1px solid black',
        'padding': '.2em',
    },

    'div': {
        'padding': '.2em'
    },
}


##############################################
# Main implementation
##############################################


class NonFormatTagCleaner(HTMLParser):
    def __init__(self, editorWebView):
        HTMLParser.__init__(self)
        self.nonAllowedTagCountInStack = 0
        self.output = []
        self.tagStack = []
        self.parseError = False
        self.editorWebView = editorWebView
        self.editor = editorWebView.editor

    def writeData(self, data):
        if self.nonAllowedTagCountInStack == 0:
            self.output.append(data)

    def handle_starttag(self, tag, attrs):
        if tag not in _voidElements:
            self.tagStack.append(tag)
            if tag not in _allowedTags:
                self.nonAllowedTagCountInStack += 1

        if tag in _ignoredTags:
            return

        # Parse attributes
        attrDict = {'style': ''}
        for k, v in attrs:
            if k in _allowedAttributes:
                attrDict[k] = v

        # Parse styles
        styleDict = {}
        for k, v in _styleRegex.findall(attrDict['style']):
            if k in _allowedStyles:
                styleDict[k] = v

        # Override styles
        if tag in _overrideStyles:
            for k, v in list(_overrideStyles[tag].items()):
                styleDict[k] = v

        if styleDict:
            attrDict['style'] = ''.join(
                "%s:%s;" % (k, v) for k, v in list(styleDict.items())
            )
        else:
            del attrDict['style']

        # Special cure for images: Download web images
        if tag == 'img' and 'src' in attrDict:
            imageUrl = attrDict['src']

            imageData = downloadMedia(imageUrl, self.editor)
            if imageData:
                fname = SaveImageToMedia(imageData, self.editor)
                attrDict['src'] = fname
            else:
                tooltip("Failed to download %s" % imageUrl)

        if attrDict:
            attrStr = ' ' + ' '.join(
                '%s="%s"' % (k, cgi.escape(v)) for k, v in list(attrDict.items())
            )
        else:
            attrStr = ''

        # Write to stack
        self.writeData("<%s%s>" % (tag, attrStr))

    def handle_endtag(self, tag):
        # Do nothing for void elements
        if tag in _voidElements:
            return

        while self.tagStack and self.tagStack[-1] != tag:
            self.tagStack.pop()
            self.parseError = True

        if self.tagStack:
            self.tagStack.pop()

        if tag in _allowedTags:
            if tag not in _ignoredTags:
                self.writeData("</%s>" % tag)
        else:
            self.nonAllowedTagCountInStack -= 1

    def handle_data(self, data):
        self.writeData(data)

    def flush(self):
        return ''.join(self.output)


_styleRegex = re.compile('(.+?) *: *(.+?);')
_allowedTags |= _ignoredTags


def SaveImageToMedia(imageData, editor):
    im = QImage.fromData(imageData)
    uname = namedtmp("pasteHTML-%d" % im.cacheKey())

    if editor.mw.pm.profile.get("pastePNG", False):
        ext = ".png"
        im.save(uname + ext, None, 50)
    else:
        ext = ".jpg"
        im.save(uname + ext, None, 80)

    # invalid image?
    if not os.path.exists(uname + ext):
        return ""

    fname = editor.mw.col.media.addFile(uname + ext)
    return fname


def cleanTag(data, editorWebView):
    parser = NonFormatTagCleaner(editorWebView)
    parser.feed(data)
    data = parser.flush()
    data = re.sub('^\s*\n', '', data, flags=re.M)
    return data


def downloadMedia(url, editor):
    # Local file : just read the file content
    if url.startswith("file://"):
        try:
            url = url[7:]
            # On windows, paths tend to be prefixed by file:///
            # rather than file://, so we remove redundant slash.
            if re.match(r'^/[A-Za-z]:\\', url):
                url = url[1:]
            return open(url, 'rb').read()
        except OSError:
            pass

    app = editor.mw.app

    # Show download dialog
    d = QDialog(editor.parentWindow)
    d.setWindowTitle("Downloading media (0.0%)")
    d.setWindowModality(Qt.WindowModal)
    vbox = QVBoxLayout()
    label = QLabel(url)
    label.setWordWrap(True)
    vbox.addWidget(label)
    d.setLayout(vbox)
    d.show()

    # Download chunk by chunk for progress bar
    try:
        response = urllib.request.urlopen(url)
        totSize = int(response.info().getheader('Content-Length').strip())
        currentRead = 0
        chunk_size = 16384
        chunks = []

        while True:
            chunk = response.read(chunk_size)
            currentRead += len(chunk)

            if not chunk:
                break

            d.setWindowTitle(
                "Downloading media (%.1f%%)" %
                (currentRead * 100.0 / totSize)
            )
            app.processEvents()
            chunks.append(chunk)

        return ''.join(chunks)

    except urllib.error.URLError:
        return None

    finally:
        d.close()
        del d


# Hook functions for EditorWebView


def buttonSetup(buttons, editor):
    icon = os.path.join(ICONS_PATH, 'paste.png')
    if sys.platform == 'darwin':  # macos
        shortcut = "Cmd+Alt+V"
    else:
        shortcut = "Ctrl+Alt+V"
    b = editor.addButton(
        icon=icon, cmd='pasteHTML',
        func=lambda editor: onHtmlCopy(editor.web),
        tip='Paste formatted HTML (%s)' % shortcut,
        keys='Ctrl+Alt+V')
    buttons.append(b)
    return buttons


def onHtmlCopy(editor_web_view):
    mode = QClipboard.Clipboard

    clip = editor_web_view.editor.mw.app.clipboard()
    mime = clip.mimeData(mode=mode)

    if mime.hasHtml():
        newMime = QMimeData()
        try:
            newHtml = cleanTag(mime.html(), editor_web_view)
        except:
            newHtml = mime.html()
        newMime.setHtml(newHtml)
        clip.setMimeData(newMime, mode=mode)
        editor_web_view.editor.doPaste(newHtml, internal=True)


addHook("setupEditorButtons", buttonSetup)
