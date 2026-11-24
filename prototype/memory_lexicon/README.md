# 中文名词词组

`common-nouns.tsv.gz` 来自 Shan Wang 与 Francis Bond 的 Chinese Open Wordnet。来源为 Open Multilingual Wordnet 提交 `406bf83b3c507a3d1f26e88252d5d66893fd36bf` 下的 `wns/cow/wn-data-cmn.tab`，原文件 SHA-256 为 `379fd2e41d3e1395f9f27cf23a39c6181849ffb4020c14a07ed2a4d4dd651122`。[原始数据](https://github.com/omwn/omw-data/tree/406bf83b3c507a3d1f26e88252d5d66893fd36bf/wns/cow) · [许可](LICENSE)

每行对应一个至少包含两个中文词的名词义项，使用制表符分隔。保留 2 至 8 个汉字的词，词与义项组排序并去重。星号表示该词在原始词表全部词性中仅有一个义项。只保留至少有一个此类词的组，共 8,404 组。运行时仅用单义查询词查本地词组。

Wang, Shan and Francis Bond. 2013. *Building the Chinese Open Wordnet (COW): Starting from Core Synsets*. Sixth International Joint Conference on Natural Language Processing, pp. 10–18. Bond, Francis and Ryan Foster. 2013. *Linking and extending an open multilingual wordnet*. ACL, pp. 1352–1362.
