import difflib
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List

from .split_by_llm import split_by_llm, MAX_WORD_COUNT
from ..bk_asr.ASRData import ASRData, from_srt, ASRDataSeg
from ..utils.logger import setup_logger

logger = setup_logger("subtitle_spliter")

SEGMENT_THRESHOLD = 500  # 每个分段的最大字数
FIXED_NUM_THREADS = 4  # 固定的线程数量
SPLIT_RANGE = 30  # 在分割点前后寻找最大时间间隔的范围
MAX_GAP = 1500.0  # 允许每个词语之间的最大时间间隔 ms

def is_pure_punctuation(s: str) -> bool:
    """
    检查字符串是否仅由标点符号组成
    """
    return not re.search(r'\w', s, flags=re.UNICODE)


def count_words(text: str) -> int:
    """
    统计多语言文本中的字符/单词数
    支持:
    - 英文（按空格分词）
    - CJK文字（中日韩统一表意文字）
    - 韩文/谚文
    - 泰文
    - 阿拉伯文
    - 俄文西里尔字母
    - 希伯来文
    - 越南文
    每个字符都计为1个单位，英文按照空格分词计数
    """
    # 定义各种语言的Unicode范围
    patterns = [
        r'[\u4e00-\u9fff]',           # 中日韩统一表意文字
        r'[\u3040-\u309f]',           # 平假名
        r'[\u30a0-\u30ff]',           # 片假名
        r'[\uac00-\ud7af]',           # 韩文音节
        r'[\u0e00-\u0e7f]',           # 泰文
        r'[\u0600-\u06ff]',           # 阿拉伯文
        r'[\u0400-\u04ff]',           # 西里尔字母（俄文等）
        r'[\u0590-\u05ff]',           # 希伯来文
        r'[\u1e00-\u1eff]',           # 越南文
        r'[\u3130-\u318f]',           # 韩文兼容字母
    ]
    
    # 统计所有非英文字符
    non_english_chars = 0
    remaining_text = text
    
    for pattern in patterns:
        # 计算当前语言的字符数
        chars = len(re.findall(pattern, remaining_text))
        non_english_chars += chars
        # 从文本中移除已计数的字符
        remaining_text = re.sub(pattern, ' ', remaining_text)
    
    # 计算英文单词数（处理剩余的文本）
    english_words = len(remaining_text.strip().split())
    
    return non_english_chars + english_words


def preprocess_text(s: str) -> str:
    """
    通过转换为小写并规范化空格来标准化文本
    """
    return ' '.join(s.lower().split())


def merge_segments_based_on_sentences(asr_data: ASRData, sentences: List[str]) -> ASRData:
    """
    基于提供的句子列表合并ASR分段
    """
    asr_texts = [seg.text for seg in asr_data.segments]
    asr_len = len(asr_texts)
    asr_index = 0  # 当前分段索引位置
    threshold = 0.5  # 相似度阈值
    max_shift = 30  # 滑动窗口的最大偏移量

    new_segments = []

    # logger.debug(f"ASR分段: {asr_texts}")

    for sentence in sentences:
        logger.debug(f"==========")
        logger.debug(f"处理句子: {sentence}")
        logger.debug("后续句子:" + "".join(asr_texts[asr_index: asr_index+10]))

        sentence_proc = preprocess_text(sentence)
        word_count = count_words(sentence_proc)
        best_ratio = 0.0
        best_pos = None
        best_window_size = 0

        # 滑动窗口大小，优先考虑接近句子词数的窗口
        max_window_size = min(word_count * 2, asr_len - asr_index)
        min_window_size = max(1, word_count // 2)
        window_sizes = sorted(range(min_window_size, max_window_size + 1), key=lambda x: abs(x - word_count))
        # logger.debug(f"window_sizes: {window_sizes}")

        for window_size in window_sizes:
            max_start = min(asr_index + max_shift + 1, asr_len - window_size + 1)
            for start in range(asr_index, max_start):
                substr = ''.join(asr_texts[start:start + window_size])
                substr_proc = preprocess_text(substr)
                ratio = difflib.SequenceMatcher(None, sentence_proc, substr_proc).ratio()
                # logger.debug(f"-----")
                # logger.debug(f"sentence_proc: {sentence_proc}, substr_proc: {substr_proc}, ratio: {ratio}")

                if ratio > best_ratio:
                    best_ratio = ratio
                    best_pos = start
                    best_window_size = window_size
                if ratio == 1.0:
                    break  # 完全匹配
            if best_ratio == 1.0:
                break  # 完全匹配

        if best_ratio >= threshold and best_pos is not None:
            start_seg_index = best_pos
            end_seg_index = best_pos + best_window_size - 1
            
            segs_to_merge = asr_data.segments[start_seg_index:end_seg_index + 1]
            seg_groups = check_time_gaps(segs_to_merge)
            # logger.debug(f"分段组: {len(seg_groups)}")

            for group in seg_groups:
                merged_text = ''.join(seg.text for seg in group)
                merged_start_time = group[0].start_time
                merged_end_time = group[-1].end_time
                merged_seg = ASRDataSeg(merged_text, merged_start_time, merged_end_time)
                
                logger.debug(f"合并分段: {merged_seg.text}")
                
                # 考虑最大词数的拆分
                if count_words(merged_text) > MAX_WORD_COUNT:
                    split_segs = split_long_segment(merged_text, group)
                    new_segments.extend(split_segs)
                else:
                    new_segments.append(merged_seg)
            max_shift = 30
            asr_index = end_seg_index + 1  # 移动到下一个未处理的分段
        else:
            logger.warning(f"无法匹配句子: {sentence}")
            max_shift = 100
            asr_index = end_seg_index + 1

    return ASRData(new_segments)


def check_time_gaps(segments: List[ASRDataSeg], max_gap: float = MAX_GAP) -> List[List[ASRDataSeg]]:
    """
    检查分段之间的时间间隔，如果超过阈值则分段
    Args:
        segments: 待检查的分段列表
        max_gap: 最大允许的时间间隔（秒）
    Returns:
        分段后的列表的列表
    """
    if not segments:
        return []
    
    result = []
    current_group = [segments[0]]
    
    for i in range(1, len(segments)):
        time_gap = segments[i].start_time - segments[i-1].end_time
        if time_gap > max_gap:
            logger.debug(f"时间间隔超过阈值: {time_gap} > {max_gap}")
            result.append(current_group)
            current_group = []
        current_group.append(segments[i])
    
    if current_group:
        result.append(current_group)
    
    return result


def split_long_segment(merged_text: str, segs_to_merge: List[ASRDataSeg]) -> List[ASRDataSeg]:
    """
    基于最大时间间隔拆分长分段，尽可能避免拆分时间连续的英文单词。
    如果所有时间间隔相等，则在中间位置断句。
    """
    result_segs = []
    logger.debug(f"正在拆分长分段: {merged_text}")

    # 基本情况：如果分段足够短或无法进一步拆分
    if count_words(merged_text) <= MAX_WORD_COUNT or len(segs_to_merge) == 1:
        merged_seg = ASRDataSeg(
            merged_text.strip(),
            segs_to_merge[0].start_time,
            segs_to_merge[-1].end_time
        )
        result_segs.append(merged_seg)
        return result_segs

    # 检查时间间隔是否都相等
    n = len(segs_to_merge)
    gaps = [segs_to_merge[i+1].start_time - segs_to_merge[i].end_time for i in range(n-1)]
    all_equal = all(abs(gap - gaps[0]) < 1e-6 for gap in gaps)

    if all_equal:
        # 如果时间间隔都相等，在中间位置断句
        split_index = n // 2
    else:
        # 在分段中间2/3部分寻找最大时间间隔点
        start_idx = n // 6
        end_idx = (5 * n) // 6
        split_index = max(
            range(start_idx, end_idx),
            key=lambda i: segs_to_merge[i + 1].start_time - segs_to_merge[i].end_time,
            default=n // 2
        )

    first_segs = segs_to_merge[:split_index + 1]
    second_segs = segs_to_merge[split_index + 1:]

    first_text = ''.join(seg.text for seg in first_segs)
    second_text = ''.join(seg.text for seg in second_segs)

    # 递归拆分
    result_segs.extend(split_long_segment(first_text, first_segs))
    result_segs.extend(split_long_segment(second_text, second_segs))

    return result_segs


def split_asr_data(asr_data: ASRData, num_segments: int) -> List[ASRData]:
    """
    根据ASR分段中的时间间隔，将ASRData拆分成多个部分。
    处理步骤：
    1. 计算总字数，并确定每个分段的字数范围。
    2. 确定平均分割点。
    3. 在分割点前后一定范围内，寻找时间间隔最大的点作为实际的分割点。
    """
    total_segs = len(asr_data.segments)
    total_word_count = count_words(asr_data.to_txt())
    words_per_segment = total_word_count // num_segments
    split_indices = []

    if num_segments <= 1 or total_segs <= num_segments:
        return [asr_data]

    # 计算每个分段的大致字数 根据每段字数计算分割点
    split_indices = [i * words_per_segment for i in range(1, num_segments)]
    # 调整分割点：在每个平均分割点附近寻找时间间隔最大的点
    adjusted_split_indices = []
    for split_point in split_indices:
        # 定义搜索范围
        start = max(0, split_point - SPLIT_RANGE)
        end = min(total_segs - 1, split_point + SPLIT_RANGE)
        # 在范围内找到时间间隔最大的点
        max_gap = -1
        best_index = split_point
        for j in range(start, end):
            gap = asr_data.segments[j + 1].start_time - asr_data.segments[j].end_time
            if gap > max_gap:
                max_gap = gap
                best_index = j
        adjusted_split_indices.append(best_index)
    # 移除重复的分割点
    adjusted_split_indices = sorted(list(set(adjusted_split_indices)))

    # 根据调整后的分割点拆分ASRData
    segments = []
    prev_index = 0
    for index in adjusted_split_indices:
        part = ASRData(asr_data.segments[prev_index:index + 1])
        segments.append(part)
        prev_index = index + 1
    # 添加最后一部分
    if prev_index < total_segs:
        part = ASRData(asr_data.segments[prev_index:])
        segments.append(part)
    return segments


def optimize_subtitles(asr_data):
    """
    优化字幕分割，合并词数少于等于3且时间相邻的段落。

    参数:
        asr_data (ASRData): 包含字幕段落的 ASRData 对象。
    """
    segments = asr_data.segments
    for i in range(len(segments) - 1, 0, -1):
        current_seg = segments[i]
        prev_seg = segments[i - 1]

        # 判断是否需要合并:
        # 1. 时间间隔小于300ms
        # 2. 当前段落词数小于5
        # 3. 合并后总词数不超过12
        time_gap = abs(current_seg.start_time - prev_seg.end_time)
        current_words = count_words(current_seg.text)
        total_words = current_words + count_words(prev_seg.text)

        if time_gap < 300 and current_words < 5 and total_words <= 12:
            asr_data.merge_with_next_segment(i - 1)
            logger.debug(f"优化：合并相邻分段: {prev_seg.text} --- {current_seg.text} -> {time_gap}")


def determine_num_segments(word_count: int, threshold: int = 1000) -> int:
    """
    根据字数计算分段数，每1000个字为一个分段，至少为1
    """
    num_segments = word_count // threshold
    # 如果存在余数，增加一个分段
    if word_count % threshold > 0:
        num_segments += 1
    return max(1, num_segments)


def merge_segments(asr_data: ASRData, model: str = "gpt-4o-mini", num_threads: int = FIXED_NUM_THREADS) -> ASRData:
    # 预处理ASR数据，去除标点并转换为小写
    new_segments = []
    for seg in asr_data.segments:
        if not is_pure_punctuation(seg.text):
            # 如果文本只包含字母和撇号，则将其转换为小写并添加一个空格
            if re.match(r'^[a-zA-Z\']+$', seg.text.strip()):
                seg.text = seg.text.lower() + " "
            new_segments.append(seg)
    asr_data.segments = new_segments

    # 获取连接后的文本
    txt = asr_data.to_txt().replace("\n", "")
    total_word_count = count_words(txt)

    # 确定分段数
    num_segments = determine_num_segments(total_word_count, threshold=SEGMENT_THRESHOLD)
    logger.info(f"根据字数 {total_word_count}，确定分段数: {num_segments}")

    # 分割ASRData
    asr_data_segments = split_asr_data(asr_data, num_segments)

    # 多线程执行 split_by_llm 获取句子列表
    logger.info("正在并行请求LLM将每个分段的文本拆分为句子...")
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        def process_segment(asr_data_part):
            txt = asr_data_part.to_txt().replace("\n", "")
            sentences = split_by_llm(txt, model=model, use_cache=False)
            logger.info(f"分段的句子提取完成，共 {len(sentences)} 句")

            return sentences

        all_sentences = list(executor.map(process_segment, asr_data_segments))
    all_sentences = [item for sublist in all_sentences for item in sublist]

    logger.info(f"总共提取到 {len(all_sentences)} 句")
    # logger.debug(f"句子列表: {all_sentences}")

    # 基于LLM已经分段的句子，对ASR分段进行合并
    logger.info("正在合并ASR分段基于句子列表...")
    merged_asr_data = merge_segments_based_on_sentences(asr_data, all_sentences)

    # 按开始时间排序合并后的分段(其实好像不需要)
    merged_asr_data.segments.sort(key=lambda seg: seg.start_time)
    final_asr_data = ASRData(merged_asr_data.segments)

    # 优化字幕分割
    optimize_subtitles(final_asr_data)

    # print(f"合并后：{final_asr_data.to_txt()}")
    return final_asr_data

def main():
    # 示例：解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(description="优化ASR分段处理脚本")
    parser.add_argument('--num_threads', type=int, default=FIXED_NUM_THREADS, help='线程数量')
    args = parser.parse_args()

    # 示例：设置文件路径
    args.srt_path = r"E:\GithubProject\VideoCaptioner\work_dir\example\subtitle\original.en.srt"
    args.save_path = args.srt_path.replace(".srt", "_merged.srt")

    # 示例：从SRT文件加载ASR数据
    with open(args.srt_path, encoding="utf-8") as f:
        asr_data = from_srt(f.read())
    logger.info(f"ASR数据加载完成，是否包含单词时间戳: {asr_data.is_word_timestamp()}")

    # 示例：合并ASR分段
    final_asr_data = merge_segments(asr_data=asr_data, num_threads=args.num_threads)

    # 示例：保存合并后的SRT文件
    final_asr_data.to_srt(save_path=args.save_path)
    logger.info(f"已保存合并后的SRT文件: {args.save_path}")

if __name__ == '__main__':
    main()
