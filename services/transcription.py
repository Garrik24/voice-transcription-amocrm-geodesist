"""
Сервис транскрибации через AssemblyAI.
Поддерживает диаризацию (разделение по говорящим).
"""
import assemblyai as aai
import logging
import tempfile
import os
from typing import Optional, List, Dict
from dataclasses import dataclass
from config import ASSEMBLYAI_API_KEY

logger = logging.getLogger(__name__)

# Настраиваем AssemblyAI
aai.settings.api_key = ASSEMBLYAI_API_KEY


@dataclass
class Speaker:
    """Информация о говорящем"""
    label: str  # A, B, C, ...
    text: str
    start_ms: int
    end_ms: int


@dataclass
class TranscriptionResult:
    """Результат транскрибации"""
    full_text: str  # Полный текст без разделения
    speakers: List[Speaker]  # Текст с разделением по говорящим
    formatted_text: str  # Форматированный текст с метками [Speaker A]: ...
    duration_seconds: float  # Длительность записи
    confidence: float  # Уверенность распознавания (0-1)
    language: str  # Определённый язык


class TranscriptionService:
    """Сервис транскрибации с диаризацией"""
    
    def __init__(self):
        self.transcriber = aai.Transcriber()
    
    async def transcribe_audio(
        self, 
        audio_data: bytes,
        language_code: str = "ru"
    ) -> TranscriptionResult:
        """
        Транскрибирует аудио с диаризацией.
        
        Args:
            audio_data: Бинарные данные аудиофайла
            language_code: Код языка (ru, en, etc.)
            
        Returns:
            Результат транскрибации с разделением по говорящим
        """
        try:
            # Сохраняем аудио во временный файл
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(audio_data)
                temp_path = f.name
            
            try:
                # Настраиваем конфигурацию транскрибации
                config = aai.TranscriptionConfig(
                    language_code=language_code,
                    speaker_labels=True,  # Включаем диаризацию!
                    punctuate=True,  # Автоматическая пунктуация
                    format_text=True,  # Форматирование текста
                )
                
                logger.info("Начинаем транскрибацию с диаризацией...")
                
                # Отправляем на транскрибацию
                transcript = self.transcriber.transcribe(temp_path, config)
                
                # Проверяем статус
                if transcript.status == aai.TranscriptStatus.error:
                    raise Exception(f"Ошибка транскрибации: {transcript.error}")
                
                # Формируем результат
                speakers = []
                formatted_lines = []
                
                if transcript.utterances:
                    # Есть разделение по говорящим
                    for utterance in transcript.utterances:
                        speaker = Speaker(
                            label=utterance.speaker,
                            text=utterance.text,
                            start_ms=utterance.start,
                            end_ms=utterance.end
                        )
                        speakers.append(speaker)
                        formatted_lines.append(f"[Говорящий {utterance.speaker}]: {utterance.text}")
                else:
                    # Нет диаризации, используем весь текст
                    formatted_lines.append(transcript.text or "")
                
                # Вычисляем длительность
                duration_seconds = 0
                if transcript.audio_duration:
                    duration_seconds = transcript.audio_duration
                elif speakers:
                    duration_seconds = speakers[-1].end_ms / 1000
                
                result = TranscriptionResult(
                    full_text=transcript.text or "",
                    speakers=speakers,
                    formatted_text="\n".join(formatted_lines),
                    duration_seconds=duration_seconds,
                    confidence=transcript.confidence or 0.0,
                    language=language_code
                )
                
                logger.info(
                    f"Транскрибация завершена: {len(result.full_text)} символов, "
                    f"{len(speakers)} фрагментов, {duration_seconds:.1f} сек"
                )
                
                return result
                
            finally:
                # Удаляем временный файл
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                    
        except Exception as e:
            logger.error(f"Ошибка транскрибации: {e}")
            raise
    
    def identify_roles(self, speakers: List[Speaker]) -> Dict[str, str]:
        """
        Пытается определить роли (менеджер/клиент) по контексту.
        
        Эвристики:
        - Первый говорящий при исходящем звонке = менеджер
        - Кто представляется компанией = менеджер
        - Кто задаёт вопросы о потребностях = менеджер
        
        Args:
            speakers: Список говорящих
            
        Returns:
            Словарь {label: role}
        """
        roles = {}
        
        # Собираем текст по каждому говорящему
        speaker_texts = {}
        for speaker in speakers:
            if speaker.label not in speaker_texts:
                speaker_texts[speaker.label] = []
            speaker_texts[speaker.label].append(speaker.text.lower())
        
        # Эвристики для определения менеджера
        manager_indicators = [
            "добрый день", "здравствуйте", "компания", "меня зовут",
            "чем могу помочь", "по поводу вашей заявки", "вы оставляли",
            "давайте", "предлагаю", "стоимость", "цена будет"
        ]
        
        client_indicators = [
            "мне нужно", "хочу", "интересует", "сколько стоит",
            "какая цена", "можете сделать", "когда сможете"
        ]
        
        for label, texts in speaker_texts.items():
            full_text = " ".join(texts)
            
            manager_score = sum(1 for ind in manager_indicators if ind in full_text)
            client_score = sum(1 for ind in client_indicators if ind in full_text)
            
            if manager_score > client_score:
                roles[label] = "Менеджер"
            else:
                roles[label] = "Клиент"
        
        # Если только 2 говорящих и не удалось определить - 
        # первый говорящий при исходящем звонке обычно менеджер
        if len(roles) == 2 and list(roles.values()).count("Менеджер") != 1:
            labels = sorted(roles.keys())
            roles[labels[0]] = "Менеджер"
            roles[labels[1]] = "Клиент"
        
        return roles
    
    def format_with_roles(
        self, 
        speakers: List[Speaker], 
        roles: Dict[str, str]
    ) -> str:
        """
        Форматирует текст с ролями вместо Speaker A/B.
        
        Args:
            speakers: Список говорящих
            roles: Словарь ролей {label: role}
            
        Returns:
            Форматированный текст
        """
        lines = []
        for speaker in speakers:
            role = roles.get(speaker.label, f"Говорящий {speaker.label}")
            lines.append(f"[{role}]: {speaker.text}")
        
        return "\n".join(lines)


# Синглтон
transcription_service = TranscriptionService()
