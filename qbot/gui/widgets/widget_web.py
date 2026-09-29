from pathlib import Path

import wx
import wx.html2 as web


class WebPanel(wx.Panel):
    def __init__(self, parent, id=-1):
        super(WebPanel, self).__init__(parent, id)

        vbox = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(vbox)
        self.browser = web.WebView.New(self)
        vbox.Add(self.browser, proportion=-1, flag=wx.EXPAND | wx.ALL, border=10)

    def show_url(self, url):
        self.browser.LoadURL(url)

    def show_file(self, filename):
        path = Path(filename)
        if not path.is_file():
            self.show_html(
                "<html><meta charset='utf-8'><body>等待生成回测报告。</body></html>"
            )
            return
        self.show_html(path.read_text(encoding="utf-8"), path.parent.as_uri() + "/")

    def show_html(self, html_content, base_url=""):
        self.browser.SetPage(html_content, base_url)
        self.browser.Show()
