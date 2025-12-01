import os
import asyncpg
from datetime import datetime

class Database:
    def __init__(self):
        self.pool = None
    
    async def connect(self):
        """Подключение к базе данных"""
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            # Для локальной разработки
            database_url = "postgresql://mexc_user@localhost/mexc_bot"
        
        self.pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        
        # Создаем таблицы если их нет
        await self.create_tables()
    
    async def create_tables(self):
        """Создаем необходимые таблицы"""
        async with self.pool.acquire() as conn:
            # Таблица блэк-листа
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS blacklist (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Таблица пауз уведомлений
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS paused_alerts (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Таблица истории алертов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS alert_history (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    prev_volume INTEGER NOT NULL,
                    curr_volume INTEGER NOT NULL,
                    prev_price FLOAT NOT NULL,
                    curr_price FLOAT NOT NULL,
                    volume_change_pct FLOAT NOT NULL,
                    price_change_pct FLOAT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Индексы для быстрого поиска
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_blacklist_symbol ON blacklist(symbol)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_paused_symbol ON paused_alerts(symbol)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_alert_history_symbol ON alert_history(symbol)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_alert_history_created ON alert_history(created_at)')
    
    async def get_blacklist(self):
        """Получить блэк-лист"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT symbol FROM blacklist')
            return {row['symbol'] for row in rows}
    
    async def add_to_blacklist(self, symbol: str):
        """Добавить в блэк-лист"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO blacklist (symbol) VALUES ($1) ON CONFLICT (symbol) DO NOTHING',
                symbol
            )
    
    async def remove_from_blacklist(self, symbol: str):
        """Удалить из блэк-листа"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM blacklist WHERE symbol = $1', symbol)
    
    async def get_paused_alerts(self):
        """Получить список пауз"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT symbol FROM paused_alerts')
            return {row['symbol'] for row in rows}
    
    async def add_paused_alert(self, symbol: str):
        """Добавить паузу"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO paused_alerts (symbol) VALUES ($1) ON CONFLICT (symbol) DO NOTHING',
                symbol
            )
    
    async def remove_paused_alert(self, symbol: str):
        """Удалить паузу"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM paused_alerts WHERE symbol = $1', symbol)
    
    async def save_alert(self, symbol: str, prev_volume: int, curr_volume: int, 
                         prev_price: float, curr_price: float, 
                         volume_change_pct: float, price_change_pct: float):
        """Сохранить алерт в историю"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO alert_history 
                (symbol, prev_volume, curr_volume, prev_price, curr_price, volume_change_pct, price_change_pct)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            ''', symbol, prev_volume, curr_volume, prev_price, curr_price, volume_change_pct, price_change_pct)
    
    async def get_recent_alerts(self, hours: int = 24):
        """Получить недавние алерты"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT symbol, prev_volume, curr_volume, volume_change_pct, price_change_pct, created_at
                FROM alert_history 
                WHERE created_at > NOW() - INTERVAL '$1 hours'
                ORDER BY created_at DESC
                LIMIT 100
            ''', hours)
            return rows
    
    async def close(self):
        """Закрыть соединение с базой"""
        if self.pool:
            await self.pool.close()

# Глобальный экземпляр базы данных
db = Database()
