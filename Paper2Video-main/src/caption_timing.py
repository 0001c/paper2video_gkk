"""把供应商/对齐器真实字词区间映射回展示原文；不插值字词时间。"""
from spoken_text import plain_text, prepare_spoken


def sentence_word_spans(texts, timing):
    characters = []
    for sentence, text in enumerate(texts):
        if 'spoken_text' in timing:
            _, mapping = prepare_spoken(text, timing.get('normalization_language', 'zh'))
        else:
            mapping = [{'original_index': index, 'original_end': index + 1, 'normalized': plain_text(char)}
                       for index, char in enumerate(text) if plain_text(char)]
        characters.extend((sentence, item['original_index'], item['original_end'], char)
                          for item in mapping for char in item['normalized'])
    words = [word for segment in timing['segments'] for word in segment['words'] if plain_text(word['word'])]
    if ''.join(char[3] for char in characters) != ''.join(plain_text(word['word']) for word in words):
        raise ValueError('字幕字词与展示原文不一致，禁止平均切分时间')
    result, position = [[] for _ in texts], 0
    for word in words:
        count = len(plain_text(word['word']))
        covered = characters[position:position + count]
        position += count
        if covered[0][0] != covered[-1][0]:
            raise ValueError('真实字词跨越讲稿句界，请合并该句后重新定位；禁止插值')
        result[covered[0][0]].append({'text_start': covered[0][1], 'text_end': covered[-1][2],
                                     'start': word['start'], 'end': word['end']})
    return result
